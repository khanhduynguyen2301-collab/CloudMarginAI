"""Clean (pre-incident) capacity + reliability generation — feeds resource_metrics_hourly.

Implements docs/phase1/workload-cost-model.md, "Capacity & reliability
generation":

    target_utilization(service) = 0.55-0.70    # policy setpoint, varies by service
    instance_count(h) = ceil(requests(h) / (capacity_per_instance × target_utilization))
                         , bounded by [min_replicas, max_replicas]
    cpu_utilization(h) = requests(h) / (instance_count(h) × capacity_per_instance) + noise
    latency_p50/p99(h) = base_latency × congestion_factor(cpu_utilization) + noise
    error_count(h)     = requests(h) × base_error_rate (Poisson), rising only
                         under injected incidents

Three conventions this module fixes, continuing demand.py's numbering:

5. `resource_metrics_hourly` is grained at RESOURCE-hour, not service-hour, so
   this returns a LONG frame: one row per (resource_id, hour). A three-region
   service yields 3x the rows of its demand frame. Region fans out here and
   nowhere earlier, because `application_activity_hourly` has no column to
   carry it — see decisions.md, "how does a service's demand fan out across
   regions".
6. `resource_id` is derived from the topology, never the rng, and always
   includes `project`. Phase 3 names a resource_id as an incident's cause, so
   the string must be reproducible from the seed alone; and prod and staging
   both contain web-frontend and checkout-api, so a name without the project
   segment collides across projects and breaks validate.py's uniqueness gate.
7. `gpu_utilization` is NaN (null) for a service with no accelerator at all, and
   0.0 for an accelerator that exists but is idle. Phase 2 must be able to tell
   those apart, because the second is the idle-accelerator incident's whole
   signature.

The autoscaler's feedback loop is deliberately tight: instance_count is derived
from requests and target_utilization, then cpu_utilization is derived back from
requests and instance_count, so utilization hovers near the setpoint and wobbles
only from ceil() rounding. Clean data therefore looks flat here, and that is
correct — a healthy autoscaler holds utilization steady. It is exactly the
invariant the autoscaling-error incident breaks (instances up, utilization
down), so do not mask the flatness with larger noise.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace

import numpy as np
import pandas as pd

from simulator.noise import unit_mean_lognormal
from simulator.topology import ResourceKind, Service

# Share of a service's demand handled by each region. Must sum to 1.0 — the
# reconciliation invariant is that request_count summed across a service's
# resources equals its demand for the same hour.
DEFAULT_REGION_WEIGHTS: dict[str, float] = {
    "us-central1": 0.50,
    "us-east1": 0.30,
    "europe-west1": 0.20,
}

# Staging runs the same services on smaller instance types with a smaller
# replica envelope. Without this, staging (at demand.STAGING_SCALE = 0.04 of
# prod demand) would sit pinned at prod's min_replicas for all 2160 hours and
# its compute cost would be perfectly flat. Staging carries no incidents
# (simulator-architecture.md), so a pinned low-weight staging region is
# acceptable; a pinned *prod* service is not.
STAGING_CAPACITY_SCALE = 0.25
STAGING_MIN_REPLICAS = 1


@dataclass(frozen=True)
class CapacityParams:
    """Per-service capacity constants.

    These live here rather than on topology.Service for the same reason
    DemandParams does: topology.py transcribes a frozen doc decision, and
    hanging tunables off it would quietly turn it into a config file.

    Fields below the divider apply only to ResourceKind.BATCH_ACCELERATOR and
    are ignored on the demand-driven path.
    """

    capacity_per_instance: float  # requests/hour one instance absorbs at 100% CPU
    target_utilization: float  # autoscaler setpoint; doc range 0.55-0.70
    min_replicas: int  # the autoscaling incident multiplies THIS 3-5x
    max_replicas: int
    base_latency_p50_ms: float
    p99_multiplier: float  # p99 = p50 * this, before congestion
    base_error_rate: float  # errors per request; Poisson mean
    memory_baseline: float  # 0-1, at the utilization setpoint
    region_weights: dict[str, float] = field(
        default_factory=lambda: dict(DEFAULT_REGION_WEIGHTS)
    )
    sigma_cpu: float = 0.03
    sigma_memory: float = 0.02
    sigma_latency: float = 0.05
    # --- BATCH_ACCELERATOR only ---
    gpu_count: int = 0
    job_window_utc: tuple[int, int] = (2, 6)  # [start, end) hour, UTC
    gpu_utilization_active: float = 0.85


# capacity_per_instance is calibrated against demand.SERVICE_DEMAND_PARAMS so no
# prod service pins at its replica floor: the smallest regional share
# (europe-west1 at 20%) must still need more than min_replicas instances at its
# weekend overnight trough, and the largest (us-central1 at 50%) must stay under
# max_replicas at its weekday peak. Retuning base_rate in demand.py without
# re-checking instance_count.nunique() here will silently flatten a service, and
# a flat instance count makes the autoscaling incident undetectable on it.
SERVICE_CAPACITY_PARAMS: dict[str, CapacityParams] = {
    "web-frontend": CapacityParams(
        capacity_per_instance=8_000,
        target_utilization=0.60,
        min_replicas=3,
        max_replicas=40,
        base_latency_p50_ms=45.0,
        p99_multiplier=3.2,
        base_error_rate=0.0008,
        memory_baseline=0.50,
    ),
    "checkout-api": CapacityParams(
        capacity_per_instance=4_000,
        target_utilization=0.60,
        min_replicas=2,
        max_replicas=30,
        base_latency_p50_ms=80.0,
        p99_multiplier=3.5,
        base_error_rate=0.0015,
        memory_baseline=0.55,
    ),
    # min_replicas is 4 rather than 2 deliberately: this is the autoscaling
    # incident's target service (topology.INCIDENT_SERVICE_MAP), and the incident
    # works by raising the floor 3-5x. With a floor of 2 against a typical 8
    # instances, a 4x raise only binds during troughs and moves mean instance
    # count just 1.13x — too weak to detect. At 4 the same raise gives 1.94x
    # instances and 0.52x CPU, a clear "instances up, utilization down"
    # signature, while clean-period variation only drops from 14 distinct values
    # to 13. Do not lower this without re-measuring the injected signature.
    "recommendation-api": CapacityParams(
        capacity_per_instance=6_000,
        target_utilization=0.65,
        min_replicas=4,
        max_replicas=30,
        base_latency_p50_ms=120.0,
        p99_multiplier=4.0,
        base_error_rate=0.0010,
        memory_baseline=0.65,
    ),
    # Queue-driven workers: a queue drains overnight rather than going idle, so
    # demand.py gives these a flatter diurnal profile and capacity follows.
    "ingestion-worker": CapacityParams(
        capacity_per_instance=5_000,
        target_utilization=0.60,
        min_replicas=2,
        max_replicas=24,
        base_latency_p50_ms=200.0,
        p99_multiplier=2.5,
        base_error_rate=0.0005,
        memory_baseline=0.45,
    ),
    "billing-worker": CapacityParams(
        capacity_per_instance=800,
        target_utilization=0.60,
        min_replicas=2,
        max_replicas=20,
        base_latency_p50_ms=350.0,
        p99_multiplier=2.2,
        base_error_rate=0.0003,
        memory_baseline=0.40,
    ),
    # Batch accelerator: instance_count and GPU utilization come off a fixed job
    # schedule, not off demand. That on/off duty cycle is what makes an idle
    # accelerator (GPUs billed, utilization near zero) read as anomalous in
    # Phase 3 rather than blending into noise.
    "ml-training-job": CapacityParams(
        capacity_per_instance=1_000,  # unused on the batch path
        target_utilization=0.60,  # unused on the batch path
        min_replicas=0,
        max_replicas=8,
        base_latency_p50_ms=0.0,
        p99_multiplier=1.0,
        base_error_rate=0.0002,
        memory_baseline=0.70,
        gpu_count=4,
        job_window_utc=(2, 6),
        gpu_utilization_active=0.85,
    ),
}


def params_for(service: Service) -> CapacityParams:
    """Resolve capacity constants for `service`, applying the staging scale."""
    try:
        base = SERVICE_CAPACITY_PARAMS[service.name]
    except KeyError as exc:
        raise KeyError(
            f"no capacity parameters for service {service.name!r} — add an entry to "
            "SERVICE_CAPACITY_PARAMS when the topology gains a service"
        ) from exc
    if service.project == "prod":
        return base
    return replace(
        base,
        capacity_per_instance=base.capacity_per_instance * STAGING_CAPACITY_SCALE,
        min_replicas=STAGING_MIN_REPLICAS,
        max_replicas=max(2, base.max_replicas // 4),
        gpu_count=max(0, base.gpu_count // 4),
    )


def resource_id_for(service: Service, region: str, ordinal: int = 1) -> str:
    """Deterministic resource identity — from the topology, never the rng.

    Includes `project`: prod and staging both contain web-frontend and
    checkout-api, so a name without that segment collides across projects and
    validate.py's uniqueness gate would fail on rows that are genuinely
    distinct. See convention (6).
    """
    return f"{service.project}-{service.name}-{region}-{ordinal:03d}"


def _resource_type_for(service: Service) -> str:
    """The `resource_type` value for schema.ResourceMetricsHourlyRow."""
    if service.resource_kind is ResourceKind.BATCH_ACCELERATOR:
        return "gpu_instance"
    return "instance"


def _demand_driven_metrics(
    params: CapacityParams,
    regional_requests: np.ndarray,
    rng: np.random.Generator,
) -> dict[str, np.ndarray]:
    """Autoscaled capacity and reliability for one service-region.

    `regional_requests` is this region's share of the service's hourly demand.
    """
    n = len(regional_requests)

    needed = regional_requests / (params.capacity_per_instance * params.target_utilization)
    instance_count = np.clip(
        np.ceil(needed), params.min_replicas, params.max_replicas
    ).astype(np.int64)

    # Derived back from the count the autoscaler chose, so utilization sits near
    # target_utilization by construction — see the module docstring on why that
    # flatness is the point rather than a defect.
    cpu = regional_requests / (instance_count * params.capacity_per_instance)
    cpu = np.clip(cpu * unit_mean_lognormal(rng, params.sigma_cpu, n), 1e-4, 1.0)

    # Memory is far less demand-elastic than CPU: a baseline plus weak coupling
    # to how far CPU has drifted from its setpoint.
    memory = params.memory_baseline + 0.25 * (cpu - params.target_utilization)
    memory = np.clip(
        memory * unit_mean_lognormal(rng, params.sigma_memory, n), 1e-4, 1.0
    )

    # Normalized so congestion == 1.0 exactly at target_utilization, climbing as
    # CPU approaches saturation. The floor stops it diverging as cpu -> 1.
    congestion = (1.0 - params.target_utilization) / np.maximum(1.0 - cpu, 0.05)
    p50 = (
        params.base_latency_p50_ms
        * congestion
        * unit_mean_lognormal(rng, params.sigma_latency, n)
    )

    return {
        "instance_count": instance_count,
        "cpu_utilization": cpu,
        "memory_utilization": memory,
        # NaN, not 0.0: this service has no accelerator at all — convention (7).
        "gpu_utilization": np.full(n, np.nan),
        "request_count": np.rint(regional_requests).astype(np.int64),
        "error_count": rng.poisson(regional_requests * params.base_error_rate).astype(
            np.int64
        ),
        "latency_p50_ms": p50,
        "latency_p99_ms": p50 * params.p99_multiplier,
    }


def _batch_accelerator_metrics(
    params: CapacityParams,
    hours: pd.DatetimeIndex,
    regional_requests: np.ndarray,
    rng: np.random.Generator,
) -> dict[str, np.ndarray]:
    """Fixed-schedule GPU capacity for a BATCH_ACCELERATOR service.

    instance_count and GPU utilization follow `job_window_utc`, not demand —
    which is what gives the idle-accelerator incident something to break.
    Outside the window the GPUs are off: instance_count 0 and gpu_utilization
    0.0, not NaN, because the accelerator exists and is merely idle.
    """
    n = len(regional_requests)
    start, end = params.job_window_utc
    hour_of_day = np.asarray(hours.hour)
    active = (hour_of_day >= start) & (hour_of_day < end)

    instance_count = np.where(active, params.gpu_count, 0).astype(np.int64)
    gpu = np.clip(
        np.where(
            active,
            params.gpu_utilization_active * unit_mean_lognormal(rng, params.sigma_cpu, n),
            0.0,
        ),
        0.0,
        1.0,
    )

    # Host CPU and memory track the GPUs they feed, at a modest level.
    cpu = np.clip(
        np.where(active, 0.35, 0.02) * unit_mean_lognormal(rng, params.sigma_cpu, n),
        1e-4,
        1.0,
    )
    memory = np.clip(
        np.where(active, params.memory_baseline, 0.10)
        * unit_mean_lognormal(rng, params.sigma_memory, n),
        1e-4,
        1.0,
    )

    return {
        "instance_count": instance_count,
        "cpu_utilization": cpu,
        "memory_utilization": memory,
        "gpu_utilization": gpu,
        "request_count": np.rint(regional_requests).astype(np.int64),
        "error_count": rng.poisson(regional_requests * params.base_error_rate).astype(
            np.int64
        ),
        # A batch job serves no requests, so request latency is undefined.
        "latency_p50_ms": np.full(n, np.nan),
        "latency_p99_ms": np.full(n, np.nan),
    }


COLUMNS: tuple[str, ...] = (
    "resource_id",
    "resource_type",
    "region",
    "hour",
    "instance_count",
    "cpu_utilization",
    "memory_utilization",
    "gpu_utilization",
    "request_count",
    "error_count",
    "latency_p50_ms",
    "latency_p99_ms",
)


def generate_capacity_and_reliability(
    service: Service,
    demand: pd.DataFrame,
    rng: np.random.Generator,
) -> pd.DataFrame:
    """Generate clean hourly resource metrics for one service from its demand.

    Args:
        service: the Service to generate for. Branches on
            `service.resource_kind`: BATCH_ACCELERATOR runs off a fixed job
            schedule, everything else off demand.
        demand: this service's frame from workloads.demand.generate_demand,
            indexed by `hour`. Only the `requests` column is read; the index
            supplies the timestamps, so the two frames cannot fall out of
            alignment.
        rng: this service's own capacity Generator — get it via
            `rng_for(master_seed, f"capacity:{service.project}:{service.name}")`.
            A separate stream from demand's, so a later change to demand's noise
            cannot shift capacity's draws. See decisions.md on per-service
            streams.

    Returns:
        Long-format frame, one row per (resource_id, hour) — 3x the rows of
        `demand` for a three-region service — with COLUMNS in that order.
        `hour` is a column rather than the index, because billing.py groups by
        (region, sku) and a flat frame is cheaper to groupby and concat.
        organization_id, project_id, service, schema_version and ingested_at are
        filled by the caller, the same way generate_demand leaves revenue and
        plan to its caller.

    Raises:
        ValueError: if the service's region weights do not sum to 1.0, which
            would break the reconciliation invariant against demand.
    """
    params = params_for(service)
    hours = demand.index
    requests = demand["requests"].to_numpy(dtype=float)

    weight_total = sum(params.region_weights.values())
    if not np.isclose(weight_total, 1.0):
        raise ValueError(
            f"region_weights for {service.name!r} sum to {weight_total}, not 1.0 — "
            "request_count would no longer reconcile against demand"
        )

    frames: list[pd.DataFrame] = []
    # Sorted so the rng is consumed in a deterministic region order, independent
    # of dict insertion order.
    for region in sorted(params.region_weights):
        regional_requests = requests * params.region_weights[region]

        if service.resource_kind is ResourceKind.BATCH_ACCELERATOR:
            metrics = _batch_accelerator_metrics(params, hours, regional_requests, rng)
        else:
            metrics = _demand_driven_metrics(params, regional_requests, rng)

        frames.append(
            pd.DataFrame(
                {
                    "resource_id": resource_id_for(service, region),
                    "resource_type": _resource_type_for(service),
                    "region": region,
                    "hour": hours,
                    **metrics,
                }
            )
        )

    return pd.concat(frames, ignore_index=True)[list(COLUMNS)]
