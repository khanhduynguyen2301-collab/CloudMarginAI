"""Clean (pre-incident) capacity + reliability generation — feeds resource_metrics_hourly.

Implements docs/phase1/workload-cost-model.md, "Capacity & reliability
generation":

    target_utilization(service) = 0.55-0.70
    instance_count(h) = ceil(requests(h) / (capacity_per_instance x target_utilization))
                         , bounded by [min_replicas, max_replicas]
    cpu_utilization(h) = requests(h) / (instance_count(h) x capacity_per_instance) + noise
    latency_p50/p99(h) = base_latency x congestion_factor(cpu_utilization) + noise
    error_count(h)     = requests(h) x base_error_rate (Poisson),
                         rising only under injected incidents

Four conventions this module fixes, on top of the four in demand.py:

5. Region fan-out is decided HERE, not in demand.py. `application_activity_hourly`
   has no region column, so demand is service-level; `resource_metrics_hourly` is
   grained at resource-hour and carries region. Each service's hourly requests are
   split across its regions by fixed weights summing to 1.0, with exactly one
   resource per service-region in v1. A variable resource pool (where
   instance_count is the pool size and resource_ids churn over time) is more
   realistic but none of the four confirmed incident types need it.
6. `resource_id` is derived from the topology, never the rng, so Phase 3 can name
   a resource as an incident's cause and have that string be reproducible. It
   includes `project`: prod and staging both contain web-frontend and
   checkout-api, so a name without it collides across projects.
7. Regions are iterated in sorted order, not dict order, so reordering
   DEFAULT_REGION_WEIGHTS cannot shift the random stream. Same spirit as the
   per-service rng decision in decisions.md.
8. BATCH_ACCELERATOR services do not fan out across regions and do not follow
   demand at all — a training job runs on a fixed schedule in one region. Their
   duty cycle is what makes an idle accelerator (GPU billed, utilization near
   zero) detectable in Phase 3 rather than indistinguishable from noise.

Returns a LONG frame — one row per (resource_id, hour) — unlike demand.py, which
returns one row per hour.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace

import numpy as np
import pandas as pd

from simulator.noise import unit_mean_lognormal
from simulator.topology import ResourceKind, Service

# Traffic distribution across the three regions in topology.py. Primary region
# first by convention, though iteration is sorted (convention 7).
DEFAULT_REGION_WEIGHTS: dict[str, float] = {
    "us-central1": 0.50,
    "us-east1": 0.30,
    "europe-west1": 0.20,
}

# A single-region weighting, for services that do not fan out (convention 8).
SINGLE_REGION_WEIGHTS: dict[str, float] = {"us-central1": 1.0}

# staging carries STAGING_SCALE (0.04) of prod's demand, so prod's replica floor
# would pin it at min_replicas for the entire run — constant instance_count,
# perfectly flat compute cost, and an undetectable autoscaling incident. Replica
# bounds are scaled too, floored so staging still has somewhere to scale.
STAGING_REPLICA_SCALE = 0.10
STAGING_MIN_REPLICAS_FLOOR = 1
STAGING_MAX_REPLICAS_FLOOR = 2

# staging also runs in ONE region rather than fanning out. Two reasons. It is
# realistic — a staging environment mirrors prod's shape, not its geography. And
# it is necessary: splitting 4% of prod's demand three ways leaves the
# lowest-weight region below one instance's worth of traffic for the whole run,
# which pins instance_count at 1 no matter how the replica bounds are scaled.
# Concentrating the traffic gives staging a band it can actually move within.


@dataclass(frozen=True)
class CapacityParams:
    """Per-service capacity constants.

    Fields below the divider apply only to ResourceKind.BATCH_ACCELERATOR and
    are ignored for demand-driven services.
    """

    capacity_per_instance: float   # requests/hour one instance absorbs at 100% CPU
    target_utilization: float      # autoscaler setpoint; doc range 0.55-0.70
    min_replicas: int              # the autoscaling incident multiplies THIS 3-5x
    max_replicas: int
    base_latency_p50_ms: float
    p99_multiplier: float          # p99 = p50 * this, before congestion
    base_error_rate: float         # errors per request; Poisson mean
    memory_baseline: float         # 0-1; memory is far less demand-elastic than CPU
    region_weights: dict[str, float] = field(
        default_factory=lambda: dict(DEFAULT_REGION_WEIGHTS)
    )
    sigma_cpu: float = 0.03
    sigma_latency: float = 0.05
    # --- BATCH_ACCELERATOR only ---
    gpu_count: int = 0
    job_window_utc: tuple[int, int] = (2, 6)   # [start, end) hour, UTC
    gpu_utilization_active: float = 0.85


# Calibrated so every demand-driven service's instance_count actually varies
# across the run. tests/test_capacity.py enforces this: a service pinned at
# min_replicas has perfectly flat compute cost, and the autoscaling-error
# incident becomes invisible on it.
SERVICE_CAPACITY_PARAMS: dict[str, CapacityParams] = {
    "web-frontend": CapacityParams(
        capacity_per_instance=3_000,
        target_utilization=0.60,
        min_replicas=4,
        max_replicas=90,
        base_latency_p50_ms=45.0,
        p99_multiplier=3.2,
        base_error_rate=0.0012,
        memory_baseline=0.45,
    ),
    "checkout-api": CapacityParams(
        capacity_per_instance=1_500,
        target_utilization=0.58,
        min_replicas=3,
        max_replicas=60,
        base_latency_p50_ms=120.0,
        p99_multiplier=4.0,
        base_error_rate=0.0025,
        memory_baseline=0.55,
    ),
    "recommendation-api": CapacityParams(
        capacity_per_instance=2_200,
        target_utilization=0.62,
        min_replicas=4,
        max_replicas=70,
        base_latency_p50_ms=80.0,
        p99_multiplier=3.5,
        base_error_rate=0.0018,
        memory_baseline=0.60,
    ),
    # Queue-driven workers: higher per-instance throughput, flatter demand, so
    # narrower replica bands.
    "ingestion-worker": CapacityParams(
        capacity_per_instance=2_500,
        target_utilization=0.68,
        min_replicas=3,
        max_replicas=40,
        base_latency_p50_ms=25.0,
        p99_multiplier=2.4,
        base_error_rate=0.0008,
        memory_baseline=0.50,
    ),
    "billing-worker": CapacityParams(
        capacity_per_instance=1_200,
        target_utilization=0.65,
        min_replicas=2,
        max_replicas=14,
        base_latency_p50_ms=200.0,
        p99_multiplier=2.8,
        base_error_rate=0.0010,
        memory_baseline=0.40,
    ),
    # Batch accelerator: schedule-driven, single region, GPU duty cycle. The
    # demand-driven fields below are unused but must be present.
    "ml-training-job": CapacityParams(
        capacity_per_instance=1_000,
        target_utilization=0.60,
        min_replicas=0,
        max_replicas=0,
        base_latency_p50_ms=0.0,
        p99_multiplier=1.0,
        base_error_rate=0.0005,
        memory_baseline=0.70,
        region_weights=dict(SINGLE_REGION_WEIGHTS),
        gpu_count=8,
        job_window_utc=(2, 6),
        gpu_utilization_active=0.85,
    ),
}

_WEIGHT_TOLERANCE = 1e-9


def resource_id_for(service: Service, region: str, ordinal: int = 1) -> str:
    """Deterministic resource identity — derived from topology, never the rng.

    Includes `project` because prod and staging both contain web-frontend and
    checkout-api; without it the IDs collide and validate.py's uniqueness gate
    would fail on rows that are genuinely distinct.
    """
    return f"{service.project}-{service.name}-{region}-{ordinal:03d}"


def generate_capacity_and_reliability(
    service: Service,
    demand: pd.DataFrame,
    rng: np.random.Generator,
) -> pd.DataFrame:
    """Generate clean hourly resource metrics for one service from its demand.

    Args:
        service: the Service (branch on `service.resource_kind` — see
            module docstring for the ml-training-job exception).
        demand: this service's output from workloads.demand.generate_demand
            (needs at least the `requests` column).
        rng: this component's Generator (same "workload" component as
            demand.py — capacity is derived from demand, not an
            independent random process, so reuse the same rng instance
            the caller passed to generate_demand for this service).

    Returns:
        DataFrame indexed by `hour` with columns: instance_count (int),
        cpu_utilization, memory_utilization (float, 0-1), gpu_utilization
        (float 0-1, or None for non-GPU services), latency_p50_ms,
        latency_p99_ms (float), request_count, error_count (int).
    """
    raise NotImplementedError("TODO: implement the capacity/reliability formulas above")
