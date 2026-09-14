"""Tests for simulator/cost_model/billing.py.

Three groups:

  * CONTRACT     — the billing_hourly schema, signs, uniqueness, fixed columns.
  * EXACTNESS    — effective_cost reconstructs from usage x price x (1 - credit).
                   This is validation-plan.md's causal gate, and it is the whole
                   reason convention 9 puts the noise on usage rather than cost.
  * MATERIALITY  — what each injected incident would actually cost, measured on
                   generated data. These are the tests that fail if a future
                   parameter retune quietly pushes an incident below the
                   detection bar. See docs/phase1/decisions.md.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from simulator.cost_model.billing import (
    CURRENCY,
    SOURCE,
    STAGING_STORAGE_SCALE,
    STORAGE_REGION,
    params_for,
    usage_to_billing_rows,
)
from simulator.cost_model.pricing import CREDIT_RATE, SKU_PRICES
from simulator.seeding import rng_for
from simulator.topology import ResourceKind, build_topology
from simulator.workloads.capacity import generate_capacity_and_reliability
from simulator.workloads.capacity import params_for as capacity_params_for
from simulator.workloads.demand import generate_demand

RUN_START = pd.Timestamp("2026-01-01", tz="UTC")
RUN_DAYS = 90

# The governance bar from docs/phase0/threat-model-governance.md: unexplained
# cost > $50/day, or > 5% of expected cost, sustained >= 3 consecutive hours.
DOLLAR_BAR_PER_DAY = 50.0


@pytest.fixture(scope="module")
def hours() -> pd.DatetimeIndex:
    return pd.date_range(RUN_START, periods=24 * RUN_DAYS, freq="h", tz="UTC")


@pytest.fixture(scope="module")
def org():
    return build_topology()


def _pipeline(service, hours, seed: int = 42):
    """demand -> capacity -> billing for one service, each on its own stream."""
    demand = generate_demand(
        service,
        hours,
        rng_for(seed, f"workload:{service.project}:{service.name}"),
        run_start=hours[0],
    )
    metrics = generate_capacity_and_reliability(
        service, demand, rng_for(seed, f"capacity:{service.project}:{service.name}")
    )
    billing = usage_to_billing_rows(
        service,
        demand,
        metrics,
        rng_for(seed, f"cost_noise:{service.project}:{service.name}"),
        run_start=hours[0],
    )
    return demand, metrics, billing


@pytest.fixture(scope="module")
def all_billing(org, hours) -> dict[tuple[str, str], pd.DataFrame]:
    return {(s.project, s.name): _pipeline(s, hours)[2] for s in org.all_services()}


def _daily(frame: pd.DataFrame, sku: str | None = None) -> float:
    """Mean $/day, optionally for one SKU."""
    subset = frame if sku is None else frame[frame.sku == sku]
    return float(subset.groupby(subset.hour.dt.date).effective_cost.sum().mean())


def _daily_usage(frame: pd.DataFrame, sku: str) -> float:
    subset = frame[frame.sku == sku]
    return float(subset.groupby(subset.hour.dt.date).usage_amount.sum().mean())


# ---------------------------------------------------------------------------
# EXACTNESS — validation-plan.md's causal gate
# ---------------------------------------------------------------------------


def test_effective_cost_reconstructs_exactly(all_billing):
    """validation-plan.md: outside incident windows, effective_cost must
    reconstruct from usage x unit_price x (1 - credit_rate). Convention 9 (noise
    on usage, never on cost) is what makes this hold to floating point."""
    for key, frame in all_billing.items():
        price = frame.sku.map(lambda s: SKU_PRICES[s].unit_price_usd)
        expected = frame.usage_amount * price * (1.0 - CREDIT_RATE)
        assert np.allclose(frame.effective_cost, expected, rtol=1e-12, atol=0.0), key


def test_credits_are_the_discount_on_list_cost(all_billing):
    """Convention 10: credits is a positive magnitude, and
    list_cost - credits == effective_cost."""
    for key, frame in all_billing.items():
        price = frame.sku.map(lambda s: SKU_PRICES[s].unit_price_usd)
        list_cost = frame.usage_amount * price
        assert np.allclose(frame.credits, list_cost * CREDIT_RATE), key
        assert np.allclose(list_cost - frame.credits, frame.effective_cost), key


# ---------------------------------------------------------------------------
# CONTRACT
# ---------------------------------------------------------------------------


def test_money_columns_are_never_negative(all_billing):
    """validation-plan.md's cost-signs gate."""
    for key, frame in all_billing.items():
        for column in ("usage_amount", "credits", "effective_cost"):
            assert (frame[column] >= 0).all(), (key, column)


def test_grain_is_unique(all_billing):
    combined = pd.concat(all_billing.values(), ignore_index=True)
    key = ["project_id", "service", "sku", "region", "hour"]
    assert not combined.duplicated(key).any()


def test_fixed_provenance_columns(all_billing):
    """Every row this simulator emits is an estimate — there is no billing
    export to reconcile against until Phase 6."""
    combined = pd.concat(all_billing.values(), ignore_index=True)
    assert set(combined.source.unique()) == {SOURCE}
    assert set(combined.currency.unique()) == {CURRENCY}
    assert not combined.is_reconciled.any()


def test_ingested_at_is_derived_from_hour_not_the_wall_clock(all_billing):
    """Convention 11. A datetime.now() here would break the byte-identical
    rerun that is Phase 1's exit gate."""
    for key, frame in all_billing.items():
        assert (frame.ingested_at == frame.hour).all(), key


def test_usage_unit_matches_the_price_table(all_billing):
    for key, frame in all_billing.items():
        for sku, group in frame.groupby("sku"):
            assert set(group.usage_unit.unique()) == {SKU_PRICES[sku].usage_unit}, (
                key,
                sku,
            )


def test_same_seed_reproduces_and_different_seed_does_not(org, hours):
    service = org.get_service("checkout-api")
    assert _pipeline(service, hours, 42)[2].equals(_pipeline(service, hours, 42)[2])
    assert not _pipeline(service, hours, 42)[2].equals(_pipeline(service, hours, 7)[2])


# ---------------------------------------------------------------------------
# CONTRACT — which SKUs a service emits
# ---------------------------------------------------------------------------


def test_batch_service_is_billed_as_gpu_hours(org, hours, all_billing):
    service = org.get_service("ml-training-job")
    assert service.resource_kind is ResourceKind.BATCH_ACCELERATOR
    skus = set(all_billing[("prod", "ml-training-job")].sku.unique())
    assert "compute.gpu-hour" in skus
    assert "compute.vcpu-hour" not in skus


def test_only_services_with_a_database_emit_db_cpu_hours(org, all_billing):
    for service in org.all_services():
        skus = set(all_billing[(service.project, service.name)].sku.unique())
        has_db = params_for(service).db_cpu_ms_per_request > 0
        assert ("db.cpu-hour" in skus) is has_db, service.name


def test_storage_is_billed_in_one_region_only(all_billing):
    """Data sits in a primary region; cross-region replication would be its own
    line item and no incident in scope needs it."""
    for key, frame in all_billing.items():
        storage = frame[frame.sku == "storage.standard-gb-month"]
        if len(storage):
            assert set(storage.region.unique()) == {STORAGE_REGION}, key


def test_gpu_hours_equal_the_running_gpu_count(org, hours):
    """usage_amount for compute.gpu-hour IS instance_count — no multiplier — so
    the idle-accelerator incident maps straight onto billed hours."""
    service = org.get_service("ml-training-job")
    _, metrics, billing = _pipeline(service, hours)
    gpu = billing[billing.sku == "compute.gpu-hour"]
    # usage carries lognormal noise; compare totals within a few percent.
    assert gpu.usage_amount.sum() == pytest.approx(
        metrics.instance_count.sum(), rel=0.02
    )


# ---------------------------------------------------------------------------
# CONTRACT — params resolution
# ---------------------------------------------------------------------------


def test_every_topology_service_has_billing_params(org):
    for service in org.all_services():
        params_for(service)


def test_params_for_rejects_unknown_service(org):
    from dataclasses import replace as dc_replace

    ghost = dc_replace(org.get_service("checkout-api"), name="does-not-exist")
    with pytest.raises(KeyError):
        params_for(ghost)


def test_staging_stores_less_but_bills_the_same_intensities(org):
    prod = params_for(org.get_service("checkout-api", project="prod"))
    staging = params_for(org.get_service("checkout-api", project="staging"))
    assert staging.storage_base_gb == pytest.approx(
        prod.storage_base_gb * STAGING_STORAGE_SCALE
    )
    assert staging.log_kb_per_request == prod.log_kb_per_request
    assert staging.db_cpu_ms_per_request == prod.db_cpu_ms_per_request


# ---------------------------------------------------------------------------
# MATERIALITY — measured, not assumed
# ---------------------------------------------------------------------------
# Each incident's weakest specified magnitude (incidents/types.py) is applied to
# the generated baseline, and the resulting $/day is checked against the
# governance bar. A retune that pushes any of these under the bar makes that
# incident undetectable in Phase 2, and should fail here rather than surface as
# mysteriously poor recall.


def test_logging_regression_clears_the_bar_at_its_weakest_magnitude(all_billing):
    """3.5x log bytes on ingestion-worker. decisions.md: 50 KB/request gives
    ~60 GB/day, so the delta is ~$72/day. At 35 KB it clears by only 4%."""
    frame = all_billing[("prod", "ingestion-worker")]
    baseline_gb = _daily_usage(frame, "logging.ingested-gb")
    price = SKU_PRICES["logging.ingested-gb"].unit_price_usd
    delta = baseline_gb * (3.5 - 1.0) * price * (1.0 - CREDIT_RATE)
    assert baseline_gb > 40.0, f"{baseline_gb:.1f} GB/day is too thin a baseline"
    assert delta > DOLLAR_BAR_PER_DAY, f"${delta:.0f}/day"


def test_query_regression_clears_the_bar_at_its_weakest_magnitude(all_billing):
    """1.8x db.cpu-hour on checkout-api."""
    frame = all_billing[("prod", "checkout-api")]
    baseline = _daily(frame, "db.cpu-hour")
    delta = baseline * (1.8 - 1.0)
    assert delta > DOLLAR_BAR_PER_DAY, f"${delta:.0f}/day"


def test_idle_accelerator_clears_the_bar(all_billing):
    """GPUs held on around the clock instead of a 4h window: +20h/day."""
    frame = all_billing[("prod", "ml-training-job")]
    baseline = _daily(frame, "compute.gpu-hour")
    delta = baseline * (24.0 / 4.0 - 1.0)
    assert delta > DOLLAR_BAR_PER_DAY, f"${delta:.0f}/day"


def _autoscaling_delta_per_day(org, hours, multiplier: int) -> float:
    """$/day added by raising recommendation-api's min_replicas.

    Only the hours where demand would have run fewer instances than the raised
    floor cost anything — at peak the autoscaler was above the floor anyway.
    """
    service = org.get_service("recommendation-api")
    _, metrics, _ = _pipeline(service, hours)
    floor = capacity_params_for(service).min_replicas * multiplier
    extra_instance_hours = float(np.maximum(floor - metrics.instance_count, 0).sum())
    vcpu = params_for(service).vcpu_per_instance
    price = SKU_PRICES["compute.vcpu-hour"].unit_price_usd
    return extra_instance_hours * vcpu * price * (1.0 - CREDIT_RATE) / RUN_DAYS


def test_autoscaling_error_clears_the_bar_at_its_strongest_magnitude(org, hours):
    """5x floor on recommendation-api — comfortably material."""
    assert _autoscaling_delta_per_day(org, hours, 5) > DOLLAR_BAR_PER_DAY


@pytest.mark.xfail(
    reason=(
        "KNOWN GAP, not a bug in this module. A 3x autoscaling error adds only "
        "~$25/day, because raising the replica floor costs nothing during hours "
        "the autoscaler was already above it. That is below the $50/day bar and "
        "below 5% of org cost (~$34/day); it is material ONLY under the "
        "service-level reading of the 5% rule, which docs/phase0/"
        "threat-model-governance.md leaves ambiguous. Resolve the OPEN entry in "
        "decisions.md, then either delete this xfail or raise the low end of the "
        "autoscaling range in incidents/types.py."
    ),
    strict=True,
)
def test_autoscaling_error_clears_the_bar_at_its_weakest_magnitude(org, hours):
    assert _autoscaling_delta_per_day(org, hours, 3) > DOLLAR_BAR_PER_DAY


# ---------------------------------------------------------------------------
# MATERIALITY — no accidental waste in the clean baseline
# ---------------------------------------------------------------------------


def test_cost_per_request_is_stable_on_clean_weekdays(org, hours):
    """The Phase 1 checklist's "cost-to-demand holds on clean periods" property.
    If unit cost drifts on clean data, Phase 2's detector learns a trend that is
    an artefact of the simulator rather than a real regression."""
    service = org.get_service("web-frontend")
    demand, _, billing = _pipeline(service, hours)

    billing_wd = billing[billing.hour.dt.dayofweek < 5]
    demand_wd = demand[demand.index.dayofweek < 5]
    cost_by_day = billing_wd.groupby(billing_wd.hour.dt.date).effective_cost.sum()
    req_by_day = demand_wd.requests.groupby(demand_wd.index.date).sum()
    unit_cost = cost_by_day / req_by_day

    assert unit_cost.std() / unit_cost.mean() < 0.05
    assert unit_cost.iloc[-5:].mean() == pytest.approx(
        unit_cost.iloc[:5].mean(), rel=0.10
    )


def test_no_service_bills_zero(all_billing):
    """A silently-zero SKU intensity would hide a whole cost line."""
    for key, frame in all_billing.items():
        assert _daily(frame) > 0, key
