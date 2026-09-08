"""Clean (pre-incident) capacity + reliability generation — feeds resource_metrics_hourly.

Implements docs/phase1/workload-cost-model.md, "Capacity & reliability
generation":

    target_utilization(service) = 0.55-0.70          # policy setpoint, varies by service
    instance_count(h) = ceil(requests(h) / (capacity_per_instance(service) * target_utilization))
                         , bounded by [min_replicas(service), max_replicas(service)]
    cpu_utilization(h) = requests(h) / (instance_count(h) * capacity_per_instance(service)) + noise
    latency_p50/p99(h) = base_latency(service) * congestion_factor(cpu_utilization) + noise
    error_count(h)     = requests(h) * base_error_rate(service) (Poisson), rising only under injected incidents

`ml-training-job` (ResourceKind.BATCH_ACCELERATOR) is the one service
whose instance_count and GPU utilization are NOT demand-driven — it runs
on a fixed job schedule. Branch on `service.resource_kind` rather than
hardcoding the service name.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from simulator.topology import Service


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
