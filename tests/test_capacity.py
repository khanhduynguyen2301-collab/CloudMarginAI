"""Tests for simulator/workloads/capacity.py.

Same split as test_demand.py: EXACT for the deterministic structure, and
STATISTICAL for what only holds in expectation. Two tests here exist
specifically to protect incident detectability in Phase 3 — see
test_instance_count_is_never_pinned and test_batch_service_has_a_duty_cycle.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from simulator.seeding import rng_for
from simulator.topology import ResourceKind, build_topology
from simulator.workloads.capacity import (
    DEFAULT_REGION_WEIGHTS,
    SERVICE_CAPACITY_PARAMS,
    SINGLE_REGION_WEIGHTS,
    generate_capacity_and_reliability,
    params_for,
    resource_id_for,
)
from simulator.workloads.demand import generate_demand

RUN_START = pd.Timestamp("2026-01-01", tz="UTC")
RUN_DAYS = 90

EXPECTED_COLUMNS = [
    "resource_id",
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
]


@pytest.fixture(scope="module")
def hours() -> pd.DatetimeIndex:
    return pd.date_range(RUN_START, periods=24 * RUN_DAYS, freq="h", tz="UTC")


@pytest.fixture(scope="module")
def org():
    return build_topology()


def _demand_for(service, hours):
    rng = rng_for(42, f"workload:{service.project}:{service.name}")
    return generate_demand(service, hours, rng, run_start=hours[0])


def _metrics_for(service, hours, seed: int = 42):
    rng = rng_for(seed, f"capacity:{service.project}:{service.name}")
    return generate_capacity_and_reliability(service, _demand_for(service, hours), rng)


def _demand_driven(org):
    return [
        s for s in org.all_services()
        if s.resource_kind is not ResourceKind.BATCH_ACCELERATOR
    ]


# ---------------------------------------------------------------------------
# EXACT — config resolution
# ---------------------------------------------------------------------------


def test_every_topology_service_has_capacity_params(org):
    """topology.py and SERVICE_CAPACITY_PARAMS are two lists that must agree."""
    for service in org.all_services():
        params_for(service)


def test_params_for_rejects_unknown_service(org):
    from dataclasses import replace as dc_replace

    ghost = dc_replace(org.get_service("checkout-api"), name="does-not-exist")
    with pytest.raises(KeyError):
        params_for(ghost)


@pytest.mark.parametrize("service_name", sorted(SERVICE_CAPACITY_PARAMS))
def test_region_weights_sum_to_one(service_name):
    weights = SERVICE_CAPACITY_PARAMS[service_name].region_weights
    assert sum(weights.values()) == pytest.approx(1.0)


def test_params_for_rejects_weights_that_do_not_sum_to_one(org):
    """The guard matters: a silent mis-weighting would stop per-resource request
    counts summing back to demand, and nothing else would notice."""
    from dataclasses import replace as dc_replace

    broken = dc_replace(
        SERVICE_CAPACITY_PARAMS["checkout-api"],
        region_weights={"us-central1": 0.5, "us-east1": 0.2},
    )
    service = org.get_service("checkout-api")
    patched = dict(SERVICE_CAPACITY_PARAMS)
    patched["checkout-api"] = broken
    import simulator.workloads.capacity as cap

    original = cap.SERVICE_CAPACITY_PARAMS
    cap.SERVICE_CAPACITY_PARAMS = patched
    try:
        with pytest.raises(ValueError, match="sum to"):
            params_for(service)
    finally:
        cap.SERVICE_CAPACITY_PARAMS = original


def test_staging_runs_in_a_single_region(org):
    """Splitting 4% of prod demand three ways pins the smallest region at one
    instance for the whole run; concentrating it gives staging a real band."""
    staging = params_for(org.get_service("web-frontend", project="staging"))
    assert staging.region_weights == SINGLE_REGION_WEIGHTS
    prod = params_for(org.get_service("web-frontend", project="prod"))
    assert prod.region_weights == DEFAULT_REGION_WEIGHTS


def test_staging_replica_bounds_are_scaled_down(org):
    prod = params_for(org.get_service("checkout-api", project="prod"))
    staging = params_for(org.get_service("checkout-api", project="staging"))
    assert staging.min_replicas <= prod.min_replicas
    assert staging.max_replicas < prod.max_replicas
    assert staging.min_replicas >= 1
    assert staging.max_replicas > staging.min_replicas


# ---------------------------------------------------------------------------
# EXACT — resource identity
# ---------------------------------------------------------------------------


def test_resource_id_includes_project_so_prod_and_staging_never_collide(org):
    """prod and staging both contain web-frontend and checkout-api."""
    prod = resource_id_for(org.get_service("web-frontend", project="prod"), "us-central1")
    staging = resource_id_for(
        org.get_service("web-frontend", project="staging"), "us-central1"
    )
    assert prod != staging


def test_resource_ids_are_globally_unique_across_the_topology(org, hours):
    seen: set[str] = set()
    for service in org.all_services():
        ids = set(_metrics_for(service, hours).resource_id.unique())
        assert not (ids & seen), f"collision introduced by {service.name}"
        seen |= ids


def test_resource_id_is_independent_of_the_rng(org, hours):
    """Phase 3 names a resource_id as an incident's cause; that string has to be
    reproducible from the topology alone."""
    service = org.get_service("checkout-api")
    assert set(_metrics_for(service, hours, seed=42).resource_id.unique()) == set(
        _metrics_for(service, hours, seed=7).resource_id.unique()
    )


# ---------------------------------------------------------------------------
# EXACT — frame contract
# ---------------------------------------------------------------------------


def test_frame_schema(org, hours):
    df = _metrics_for(org.get_service("checkout-api"), hours)
    assert list(df.columns) == EXPECTED_COLUMNS
    for column in ("instance_count", "request_count", "error_count"):
        assert np.issubdtype(df[column].dtype, np.integer), column


def test_row_count_is_one_per_resource_hour(org, hours):
    for service in org.all_services():
        df = _metrics_for(service, hours)
        n_regions = len(params_for(service).region_weights)
        assert len(df) == len(hours) * n_regions, service.name
        assert df.resource_id.nunique() == n_regions, service.name


def test_utilizations_stay_in_range(org, hours):
    for service in org.all_services():
        df = _metrics_for(service, hours)
        for column in ("cpu_utilization", "memory_utilization"):
            assert (df[column] >= 0).all(), (service.name, column)
            assert (df[column] <= 1.0).all(), (service.name, column)


def test_instance_count_respects_its_bounds(org, hours):
    for service in _demand_driven(org):
        params = params_for(service)
        df = _metrics_for(service, hours)
        assert df.instance_count.min() >= params.min_replicas, service.name
        assert df.instance_count.max() <= params.max_replicas, service.name


def test_same_seed_reproduces_and_different_seed_does_not(org, hours):
    service = org.get_service("checkout-api")
    assert _metrics_for(service, hours, 42).equals(_metrics_for(service, hours, 42))
    assert not _metrics_for(service, hours, 42).equals(_metrics_for(service, hours, 7))


def test_region_iteration_order_does_not_depend_on_dict_order(org, hours):
    """Regions are iterated sorted(), so reordering DEFAULT_REGION_WEIGHTS cannot
    shift the random stream."""
    import simulator.workloads.capacity as cap

    service = org.get_service("web-frontend")
    before = _metrics_for(service, hours)
    original = cap.DEFAULT_REGION_WEIGHTS
    cap.DEFAULT_REGION_WEIGHTS = dict(reversed(list(original.items())))
    try:
        after = _metrics_for(service, hours)
    finally:
        cap.DEFAULT_REGION_WEIGHTS = original
    assert before.equals(after)


# ---------------------------------------------------------------------------
# EXACT — the two invariants that protect Phase 3
# ---------------------------------------------------------------------------


def test_instance_count_is_never_pinned(org, hours):
    """A service stuck at min_replicas has perfectly flat compute cost, which
    makes the autoscaling-error incident undetectable on it. This is the test
    that catches a mis-calibrated capacity_per_instance or replica floor."""
    for service in _demand_driven(org):
        df = _metrics_for(service, hours)
        varies = df.groupby("resource_id").instance_count.nunique()
        assert varies.min() > 1, f"{service.project}/{service.name} pinned: {dict(varies)}"


def test_batch_service_has_a_duty_cycle(org, hours):
    """The idle-accelerator incident works by holding GPUs active with
    utilization near zero, so the clean baseline needs a crisp on/off rhythm."""
    service = org.get_service("ml-training-job")
    assert service.resource_kind is ResourceKind.BATCH_ACCELERATOR
    df = _metrics_for(service, hours)
    params = params_for(service)

    running = df[df.instance_count > 0]
    stopped = df[df.instance_count == 0]
    assert len(running) > 0 and len(stopped) > 0
    assert running.instance_count.unique().tolist() == [params.gpu_count]
    assert running.gpu_utilization.min() > 0.5
    assert (stopped.gpu_utilization == 0.0).all()

    start, end = params.job_window_utc
    assert set(running.hour.dt.hour.unique()) == set(range(start, end))


def test_batch_service_is_not_demand_driven(org, hours):
    """Its instance_count must not move with requests, or an idle accelerator
    becomes indistinguishable from a quiet hour."""
    service = org.get_service("ml-training-job")
    df = _metrics_for(service, hours)
    demand = _demand_for(service, hours)
    merged = df.set_index("hour").join(demand[["requests"]])
    inside = merged[merged.instance_count > 0]
    assert inside.instance_count.nunique() == 1


def test_gpu_utilization_is_null_only_for_non_gpu_services(org, hours):
    """NaN means 'no GPU at all'; 0.0 means 'a GPU that exists and is idle'.
    Phase 2 will treat those differently."""
    for service in _demand_driven(org):
        df = _metrics_for(service, hours)
        assert df.gpu_utilization.isna().all(), service.name
    batch = _metrics_for(org.get_service("ml-training-job"), hours)
    assert batch.gpu_utilization.notna().all()


# ---------------------------------------------------------------------------
# STATISTICAL / reconciliation
# ---------------------------------------------------------------------------


def test_region_split_reconciles_to_demand(org, hours):
    """Per-hour request_count summed across a service's resources matches that
    hour's demand within the rounding tolerance. Catches weights that drift."""
    for service in _demand_driven(org):
        df = _metrics_for(service, hours)
        demand = _demand_for(service, hours)
        summed = df.groupby("hour").request_count.sum()
        drift = (summed - demand.requests).abs().max()
        assert drift <= len(params_for(service).region_weights), service.name


def test_cpu_hovers_near_the_autoscaler_setpoint(org, hours):
    """The circular definition is deliberate: instance_count is chosen to hold
    utilization at target, so clean-period CPU should sit near it. This is the
    baseline the autoscaling incident departs from."""
    for service in _demand_driven(org):
        params = params_for(service)
        df = _metrics_for(service, hours)
        assert df.cpu_utilization.median() == pytest.approx(
            params.target_utilization, rel=0.25
        ), service.name


def test_latency_rises_with_cpu_pressure(org, hours):
    """Congestion is the only thing moving latency on clean data, so the two
    must correlate positively."""
    df = _metrics_for(org.get_service("checkout-api"), hours)
    assert df.cpu_utilization.corr(df.latency_p50_ms) > 0.5


def test_p99_exceeds_p50(org, hours):
    for service in _demand_driven(org):
        df = _metrics_for(service, hours)
        assert (df.latency_p99_ms > df.latency_p50_ms).all(), service.name


def test_error_rate_is_low_and_roughly_flat(org, hours):
    """None of the four incident types injects errors, so errors must not be a
    confounding signal on clean data."""
    df = _metrics_for(org.get_service("checkout-api"), hours)
    rate = df.error_count.sum() / df.request_count.sum()
    expected = SERVICE_CAPACITY_PARAMS["checkout-api"].base_error_rate
    assert rate == pytest.approx(expected, rel=0.10)