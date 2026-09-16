"""Tests for simulator/incidents/engine.py.

Same CONTRACT / DISTRIBUTION split as the other test modules. The contract
group is single-seed safe; the distribution group aggregates because duration
and magnitude are drawn per incident.

Three groups of properties matter more than the rest.

Purity and containment (convention 14): `inject` must not modify the frames it
is handed, and must not change a single value outside its window. Both are what
let the ground-truth magnitude be measured against the clean series, and both
are the kind of bug that produces a plausible-looking dataset with a silent
leak in it.

The per-type signatures. Each incident type is supposed to move a specific,
different set of columns - and leave the rest exactly alone. logging_regression
in particular must not touch `metrics` at all, because "cost moved and nothing
else did" is the whole reason it is the M2 milestone.

Materiality. An injected incident that does not clear
docs/phase0/threat-model-governance.md's bar is ground truth a detector is
right to ignore, which makes it a mislabelled row rather than a hard case.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from simulator.cost_model.billing import usage_to_billing_rows
from simulator.cost_model.pricing import CREDIT_RATE, SKU_PRICES
from simulator.incidents.engine import (
    CAUSAL_DEPLOYMENT_ID_OFFSET,
    CAUSAL_EVENT_TAG,
    CAUSAL_LEAD_HOURS_HIGH,
    CAUSAL_LEAD_HOURS_LOW,
    MIN_GAP_HOURS_SAME_SERVICE,
    SPLIT_ALLOCATION,
    SPLIT_ORDER,
    inject,
    place_incidents,
    window_end,
)
from simulator.incidents.types import INCIDENT_SPECS, IncidentType
from simulator.schema import DeploymentRow, ResourceChangeRow
from simulator.seeding import rng_for
from simulator.topology import ORGANIZATION_ID, build_topology
from simulator.workloads.capacity import generate_capacity_and_reliability
from simulator.workloads.capacity import params_for as capacity_params_for
from simulator.workloads.demand import generate_demand

RUN_START = pd.Timestamp("2026-01-01", tz="UTC")
RUN_DAYS = 90
SEED = 42
SEEDS_FOR_DISTRIBUTION = 25

# docs/phase0/threat-model-governance.md
MATERIALITY_USD_PER_DAY = 50.0
MATERIALITY_ORG_SHARE = 0.05

EXPECTED_MAGNITUDE_KEYS = {
    IncidentType.LOGGING_REGRESSION: {"log_byte_multiplier"},
    IncidentType.QUERY_REGRESSION: {"latency_delta_pct", "cost_delta_pct"},
    IncidentType.IDLE_ACCELERATOR: {"idle_cost_rate"},
    IncidentType.AUTOSCALING_ERROR: {"instance_count_delta"},
}


@pytest.fixture(scope="module")
def hours() -> pd.DatetimeIndex:
    return pd.date_range(RUN_START, periods=24 * RUN_DAYS, freq="h", tz="UTC")


@pytest.fixture(scope="module")
def org():
    return build_topology()


@pytest.fixture(scope="module")
def bounds(hours):
    """60/15/15 chronological splits, half-open [start, end)."""
    start = hours[0]
    return {
        "train": (start, start + pd.Timedelta(days=60)),
        "validation": (start + pd.Timedelta(days=60), start + pd.Timedelta(days=75)),
        "test": (start + pd.Timedelta(days=75), start + pd.Timedelta(days=90)),
    }


def _clean_run(org, hours, seed: int) -> dict:
    """Every service's clean demand/metrics/billing for one seed.

    Keyed by (project, name), NOT name: prod and staging both contain
    web-frontend and checkout-api, so a name-only key silently overwrites prod
    with staging - and checkout-api is query_regression's target.
    """
    out = {}
    for s in org.all_services():
        d = generate_demand(
            s, hours, rng_for(seed, f"workload:{s.project}:{s.name}"), run_start=hours[0]
        )
        m = generate_capacity_and_reliability(
            s, d, rng_for(seed, f"capacity:{s.project}:{s.name}")
        )
        b = usage_to_billing_rows(
            s, d, m, rng_for(seed, f"cost_noise:{s.project}:{s.name}"), run_start=hours[0]
        )
        out[(s.project, s.name)] = (d, m, b)
    return out


@pytest.fixture(scope="module")
def clean(org, hours):
    return _clean_run(org, hours, SEED)


def _split_of(bounds, start) -> str:
    return next(name for name, (a, b) in bounds.items() if a <= start < b)


def _inject_all(org, hours, bounds, clean, seed: int = SEED) -> list[dict]:
    """Place and inject a whole run, returning one record per incident."""
    rng = rng_for(seed, "incidents")
    plan = place_incidents(org, hours, bounds, rng)
    records = []
    for ordinal, (kind, service, start, duration, magnitude) in enumerate(plan, start=1):
        d, m, b = clean[(service.project, service.name)]
        split = _split_of(bounds, start)
        d2, m2, b2, causal, gt = inject(
            kind,
            service,
            start,
            duration,
            magnitude,
            d,
            m,
            b,
            rng,
            organization_id=ORGANIZATION_ID,
            chronological_split=split,
            simulator_seed=seed,
            ordinal=ordinal,
        )
        records.append(
            {
                "kind": kind,
                "service": service,
                "start": start,
                "duration": duration,
                "magnitude": magnitude,
                "split": split,
                "clean_metrics": m,
                "clean_billing": b,
                "metrics": m2,
                "billing": b2,
                "demand_in": d,
                "demand_out": d2,
                "causal": causal,
                "gt": gt,
            }
        )
    return records


@pytest.fixture(scope="module")
def run(org, hours, bounds, clean):
    return _inject_all(org, hours, bounds, clean)


def _in_window(frame: pd.DataFrame, start, duration: int) -> np.ndarray:
    hour = pd.DatetimeIndex(frame["hour"])
    return np.asarray((hour >= start) & (hour < window_end(start, duration)))


# ---------------------------------------------------------------------------
# CONTRACT — placement
# ---------------------------------------------------------------------------


def test_allocation_matches_the_spec_as_a_constants_guard():
    """A guard on SPLIT_ALLOCATION itself, independent of any generated run:
    10 incidents, 6/2/2, 2-3 per type, exactly one logging_regression in test."""
    per_split = {k: len(v) for k, v in SPLIT_ALLOCATION.items()}
    assert per_split == {"train": 6, "validation": 2, "test": 2}
    assert set(SPLIT_ALLOCATION) == set(SPLIT_ORDER)

    flat = [t for split in SPLIT_ORDER for t in SPLIT_ALLOCATION[split]]
    assert len(flat) == 10
    for kind in IncidentType:
        assert 2 <= flat.count(kind) <= 3, kind
    assert SPLIT_ALLOCATION["test"].count(IncidentType.LOGGING_REGRESSION) == 1


def test_places_ten_incidents_with_the_spec_counts(org, hours, bounds):
    plan = place_incidents(org, hours, bounds, rng_for(SEED, "incidents"))
    assert len(plan) == 10
    by_split: dict[str, list] = {}
    for kind, _, start, _, _ in plan:
        by_split.setdefault(_split_of(bounds, start), []).append(kind)
    assert {k: len(v) for k, v in by_split.items()} == {
        "train": 6,
        "validation": 2,
        "test": 2,
    }
    assert by_split["test"].count(IncidentType.LOGGING_REGRESSION) == 1


@pytest.mark.parametrize("seed", range(12))
def test_no_window_crosses_a_split_boundary(org, hours, bounds, seed):
    """Half of the point of the splits. A window straddling train/test leaks a
    labelled incident into held-out data, and Phase 2's backtest stops meaning
    anything."""
    for kind, _, start, duration, _ in place_incidents(
        org, hours, bounds, rng_for(seed, "incidents")
    ):
        end = window_end(start, duration)
        fits = [name for name, (a, b) in bounds.items() if a <= start and end <= b]
        assert len(fits) == 1, (seed, kind, start, duration)


@pytest.mark.parametrize("seed", range(12))
def test_same_service_windows_are_separated(org, hours, bounds, seed):
    """Not merely non-overlapping. Two windows flush against each other read as
    one long incident with a dip, which Phase 2's window labelling cannot split."""
    windows: dict[str, list[tuple]] = {}
    for _, service, start, duration, _ in place_incidents(
        org, hours, bounds, rng_for(seed, "incidents")
    ):
        windows.setdefault(service.name, []).append((start, window_end(start, duration)))
    gap = pd.Timedelta(hours=MIN_GAP_HOURS_SAME_SERVICE)
    for name, spans in windows.items():
        spans.sort()
        for (_, prev_end), (next_start, _) in zip(spans, spans[1:]):
            assert next_start - prev_end >= gap, name


def test_starts_land_on_run_hours_with_room_for_the_causal_lead(org, hours, bounds):
    """The causal event is placed before the incident starts, so placement has to
    leave room for it inside the split - otherwise a deploy lands in the
    previous split, or before the run begins."""
    valid = set(hours)
    for _, _, start, _, _ in place_incidents(org, hours, bounds, rng_for(SEED, "incidents")):
        assert start in valid
        split_start = bounds[_split_of(bounds, start)][0]
        assert start - pd.Timedelta(hours=CAUSAL_LEAD_HOURS_HIGH) >= split_start


def test_durations_and_magnitudes_stay_inside_their_spec_bands(org, hours, bounds):
    for kind, _, _, duration, magnitude in place_incidents(
        org, hours, bounds, rng_for(SEED, "incidents")
    ):
        spec = INCIDENT_SPECS[kind]
        assert spec.duration_hours_low <= duration <= spec.duration_hours_high
        assert spec.magnitude_low <= magnitude <= spec.magnitude_high


def test_each_incident_lands_on_its_home_service(org, hours, bounds):
    for kind, service, _, _, _ in place_incidents(org, hours, bounds, rng_for(SEED, "incidents")):
        assert service.name == INCIDENT_SPECS[kind].service_name
        assert service.project == "prod"


def test_placement_reproduces_and_different_seed_does_not(org, hours, bounds):
    a = place_incidents(org, hours, bounds, rng_for(SEED, "incidents"))
    b = place_incidents(org, hours, bounds, rng_for(SEED, "incidents"))
    c = place_incidents(org, hours, bounds, rng_for(7, "incidents"))
    assert a == b
    assert a != c


def test_missing_split_boundary_raises(org, hours, bounds):
    partial = {k: v for k, v in bounds.items() if k != "test"}
    with pytest.raises(KeyError, match="test"):
        place_incidents(org, hours, partial, rng_for(SEED, "incidents"))


# ---------------------------------------------------------------------------
# CONTRACT — purity and containment (convention 14)
# ---------------------------------------------------------------------------


def test_inject_does_not_modify_its_inputs(run, clean):
    """Convention 14. The clean series has to survive the injection, or the
    measured magnitudes in convention 15 are measured against themselves."""
    for r in run:
        key = (r["service"].project, r["service"].name)
        assert r["clean_metrics"].equals(clean[key][1]), key
        assert r["clean_billing"].equals(clean[key][2]), key
        assert r["metrics"] is not r["clean_metrics"]
        assert r["billing"] is not r["clean_billing"]


def test_demand_is_returned_untouched(run):
    """Convention 17. No incident type changes traffic, so
    application_activity_hourly is a clean control series for the whole run -
    which is what denies Phase 3 a business-demand shortcut."""
    for r in run:
        assert r["demand_out"] is r["demand_in"]


def test_request_count_never_changes(run):
    for r in run:
        assert np.array_equal(
            r["metrics"]["request_count"].to_numpy(),
            r["clean_metrics"]["request_count"].to_numpy(),
        ), r["kind"]


def test_nothing_outside_the_window_changes(run):
    """Containment. A mutation that leaks past its window quietly contaminates
    the clean baseline a detector calibrates against."""
    for r in run:
        for key, clean_key in (("metrics", "clean_metrics"), ("billing", "clean_billing")):
            outside = ~_in_window(r[clean_key], r["start"], r["duration"])
            assert r[key][outside].equals(r[clean_key][outside]), (r["kind"], key)


def test_injecting_on_the_wrong_service_raises(org, hours, bounds, clean):
    wrong = org.get_service("billing-worker")
    d, m, b = clean[("prod", "billing-worker")]
    with pytest.raises(ValueError, match="ingestion-worker"):
        inject(
            IncidentType.LOGGING_REGRESSION,
            wrong,
            hours[100],
            72,
            4.0,
            d,
            m,
            b,
            rng_for(SEED, "incidents"),
            organization_id=ORGANIZATION_ID,
            chronological_split="train",
            simulator_seed=SEED,
        )


# ---------------------------------------------------------------------------
# CONTRACT — billing invariants survive injection
# ---------------------------------------------------------------------------


def test_effective_cost_still_reconstructs_exactly(run):
    """billing.py's convention 9, re-checked INSIDE incident windows. engine.py
    rebuilds cost from usage rather than scaling three columns in step
    (convention 16), so this must hold to floating point."""
    for r in run:
        b = r["billing"]
        price = np.array([SKU_PRICES[s].unit_price_usd for s in b["sku"]], dtype=float)
        expected = b["usage_amount"].to_numpy(dtype=float) * price * (1.0 - CREDIT_RATE)
        assert np.abs(expected - b["effective_cost"].to_numpy()).max() < 1e-9, r["kind"]


def test_cost_signs_still_hold(run):
    """validation-plan.md's cost-signs gate: usage, credits and effective_cost
    are all non-negative, incident or not."""
    for r in run:
        b = r["billing"]
        for column in ("usage_amount", "credits", "effective_cost"):
            assert (b[column].to_numpy() >= 0).all(), (r["kind"], column)


def test_every_incident_raises_cost(run):
    """All four types are cost incidents. One that lowered spend would be a sign
    inversion, and the ground-truth row would be actively misleading."""
    for r in run:
        rows = _in_window(r["clean_billing"], r["start"], r["duration"])
        before = r["clean_billing"].loc[rows, "effective_cost"].sum()
        after = r["billing"].loc[rows, "effective_cost"].sum()
        assert after > before, r["kind"]


# ---------------------------------------------------------------------------
# CONTRACT — per-type signatures
# ---------------------------------------------------------------------------


def _records(run, kind):
    return [r for r in run if r["kind"] is kind]


def test_logging_regression_leaves_metrics_completely_untouched(run):
    """The M2 signature: cost moves and NOTHING else does. If any resource
    metric shifted, Phase 2 could find this incident without the cost series,
    and the milestone would be testing the wrong thing."""
    for r in _records(run, IncidentType.LOGGING_REGRESSION):
        assert r["metrics"].equals(r["clean_metrics"])


def test_logging_regression_scales_only_its_own_sku(run):
    for r in _records(run, IncidentType.LOGGING_REGRESSION):
        window = _in_window(r["clean_billing"], r["start"], r["duration"])
        target = window & (r["clean_billing"]["sku"] == "logging.ingested-gb").to_numpy()
        ratio = (
            r["billing"].loc[target, "usage_amount"].to_numpy()
            / r["clean_billing"].loc[target, "usage_amount"].to_numpy()
        )
        assert np.allclose(ratio, r["magnitude"])
        others = window & ~target
        assert r["billing"][others].equals(r["clean_billing"][others])


def test_query_regression_scales_db_cpu_and_nothing_else(run):
    for r in _records(run, IncidentType.QUERY_REGRESSION):
        window = _in_window(r["clean_billing"], r["start"], r["duration"])
        target = window & (r["clean_billing"]["sku"] == "db.cpu-hour").to_numpy()
        ratio = (
            r["billing"].loc[target, "usage_amount"].to_numpy()
            / r["clean_billing"].loc[target, "usage_amount"].to_numpy()
        )
        assert np.allclose(ratio, r["magnitude"])
        others = window & ~target
        assert r["billing"][others].equals(r["clean_billing"][others])


def test_query_regression_widens_the_latency_tail(run):
    """p99 takes the full multiplier, p50 its square root - so the p99/p50 ratio
    grows. A flat multiplier on both would leave the tail shape unchanged, which
    is not what a slow query looks like, and would cost Phase 3 a real feature."""
    for r in _records(run, IncidentType.QUERY_REGRESSION):
        rows = _in_window(r["clean_metrics"], r["start"], r["duration"])
        p99 = (
            r["metrics"].loc[rows, "latency_p99_ms"].to_numpy()
            / r["clean_metrics"].loc[rows, "latency_p99_ms"].to_numpy()
        )
        p50 = (
            r["metrics"].loc[rows, "latency_p50_ms"].to_numpy()
            / r["clean_metrics"].loc[rows, "latency_p50_ms"].to_numpy()
        )
        assert np.allclose(p50**2, p99)
        assert p99.min() > 1.0
        # the ground-truth row has to agree with the data it describes
        assert np.allclose(p99, 1.0 + r["gt"].expected_magnitude["latency_delta_pct"] / 100.0)


def test_query_regression_leaves_utilization_alone(run):
    """A query regression burns DB CPU, not application CPU. Utilization holding
    steady while cost and latency both climb is what distinguishes it from a
    traffic surge."""
    for r in _records(run, IncidentType.QUERY_REGRESSION):
        assert np.array_equal(
            r["metrics"]["cpu_utilization"].to_numpy(),
            r["clean_metrics"]["cpu_utilization"].to_numpy(),
        )
        assert np.array_equal(
            r["metrics"]["instance_count"].to_numpy(),
            r["clean_metrics"]["instance_count"].to_numpy(),
        )


def test_idle_accelerator_holds_the_gpus_on_and_empty(run):
    for r in _records(run, IncidentType.IDLE_ACCELERATOR):
        params = capacity_params_for(r["service"])
        rows = _in_window(r["clean_metrics"], r["start"], r["duration"])
        assert (r["metrics"].loc[rows, "instance_count"] == params.gpu_count).all()
        gpu = r["metrics"].loc[rows, "gpu_utilization"].to_numpy()
        assert (gpu >= 0).all() and (gpu <= 0.05).all()


def test_idle_accelerator_bills_the_hours_the_job_should_have_been_off(run):
    """Where the money actually goes. In the clean frame those hours bill zero
    GPU-hours; under the incident they bill the full pool."""
    for r in _records(run, IncidentType.IDLE_ACCELERATOR):
        window = _in_window(r["clean_billing"], r["start"], r["duration"])
        gpu = window & (r["clean_billing"]["sku"] == "compute.gpu-hour").to_numpy()
        was_zero = r["clean_billing"].loc[gpu, "usage_amount"].to_numpy() == 0.0
        assert was_zero.any(), "window contained no off-hours to bill"
        assert (r["billing"].loc[gpu, "usage_amount"].to_numpy()[was_zero] > 0).all()
        # hours the job was genuinely running are billed the same as before
        assert np.allclose(
            r["billing"].loc[gpu, "usage_amount"].to_numpy()[~was_zero],
            r["clean_billing"].loc[gpu, "usage_amount"].to_numpy()[~was_zero],
        )


def test_autoscaling_error_raises_instances_while_cpu_and_latency_fall(run):
    """The correlated signature, and the reason this type is not just "cost went
    up": instances rise, utilization falls because CPU is derived from them, and
    latency IMPROVES. A traffic surge moves all three the other way."""
    for r in _records(run, IncidentType.AUTOSCALING_ERROR):
        params = capacity_params_for(r["service"])
        floor = min(int(round(params.min_replicas * r["magnitude"])), params.max_replicas)
        rows = _in_window(r["clean_metrics"], r["start"], r["duration"])

        new_i = r["metrics"].loc[rows, "instance_count"].to_numpy()
        old_i = r["clean_metrics"].loc[rows, "instance_count"].to_numpy()
        assert (new_i >= np.minimum(floor, params.max_replicas)).all()
        assert (new_i >= old_i).all()
        assert (new_i > old_i).any(), "the raised floor lifted no hour at all"

        lifted = new_i > old_i
        new_cpu = r["metrics"].loc[rows, "cpu_utilization"].to_numpy()
        old_cpu = r["clean_metrics"].loc[rows, "cpu_utilization"].to_numpy()
        assert (new_cpu[lifted] < old_cpu[lifted]).all()

        new_p99 = r["metrics"].loc[rows, "latency_p99_ms"].to_numpy()
        old_p99 = r["clean_metrics"].loc[rows, "latency_p99_ms"].to_numpy()
        assert (new_p99[lifted] <= old_p99[lifted]).all()


def test_autoscaling_error_scales_vcpu_usage_by_the_instance_ratio(run):
    """billing.py builds vCPU-hours from instance_count, so the two have to move
    together. Drift here would mean cost and capacity telling different stories
    about the same hour."""
    for r in _records(run, IncidentType.AUTOSCALING_ERROR):
        rows = _in_window(r["clean_metrics"], r["start"], r["duration"])
        instance_ratio = pd.Series(
            r["metrics"].loc[rows, "instance_count"].to_numpy(dtype=float)
            / r["clean_metrics"].loc[rows, "instance_count"].to_numpy(dtype=float),
            index=pd.MultiIndex.from_arrays(
                [
                    r["clean_metrics"].loc[rows, "region"].to_numpy(),
                    pd.DatetimeIndex(r["clean_metrics"].loc[rows, "hour"]),
                ]
            ),
        )
        window = _in_window(r["clean_billing"], r["start"], r["duration"])
        cpu = window & (r["clean_billing"]["sku"] == "compute.vcpu-hour").to_numpy()
        key = pd.MultiIndex.from_arrays(
            [
                r["clean_billing"].loc[cpu, "region"].to_numpy(),
                pd.DatetimeIndex(r["clean_billing"].loc[cpu, "hour"]),
            ]
        )
        usage_ratio = (
            r["billing"].loc[cpu, "usage_amount"].to_numpy()
            / r["clean_billing"].loc[cpu, "usage_amount"].to_numpy()
        )
        assert np.allclose(usage_ratio, instance_ratio.reindex(key).to_numpy())


# ---------------------------------------------------------------------------
# CONTRACT — causal events
# ---------------------------------------------------------------------------


def test_causal_event_carries_the_reserved_value_for_its_type(run):
    """The other side of changes/ convention 12. Background generators never
    write these; this is the only place they appear."""
    for r in run:
        table, reserved = CAUSAL_EVENT_TAG[r["kind"]]
        causal = r["causal"]
        if table == "deployments":
            assert isinstance(causal, DeploymentRow)
            assert causal.changed_component == reserved
        else:
            assert isinstance(causal, ResourceChangeRow)
            assert causal.change_type == reserved


def test_causal_event_precedes_its_incident_within_the_lead_band(run):
    """Never at the same instant. A zero lag would let Phase 3 find the true
    cause with an equality join on the timestamp instead of by evidence."""
    for r in run:
        causal = r["causal"]
        at = getattr(causal, "released_at", None) or causal.changed_at
        lead = (r["start"] - at) / pd.Timedelta(hours=1)
        assert CAUSAL_LEAD_HOURS_LOW <= lead <= CAUSAL_LEAD_HOURS_HIGH, r["kind"]


def test_causal_deployment_ids_cannot_collide_with_background_ones(run):
    """deployment_id is a primary key minted by two generators that never see
    each other. The offset is what keeps them apart until cli.py renumbers."""
    for r in run:
        if isinstance(r["causal"], DeploymentRow):
            ordinal = int(r["causal"].deployment_id.rsplit("-", 1)[1])
            assert ordinal > CAUSAL_DEPLOYMENT_ID_OFFSET


def test_causal_config_change_names_a_real_resource(run, clean):
    for r in run:
        if isinstance(r["causal"], ResourceChangeRow):
            key = (r["service"].project, r["service"].name)
            real = set(clean[key][1]["resource_id"].unique())
            assert r["causal"].resource_id in real
            assert r["causal"].before_config != r["causal"].after_config


def test_causal_ingested_at_is_the_event_time(run):
    """Convention 11 again - a datetime.now() here breaks the byte-identical
    rerun that is Phase 1's exit gate."""
    for r in run:
        causal = r["causal"]
        at = getattr(causal, "released_at", None) or causal.changed_at
        assert causal.ingested_at == at


# ---------------------------------------------------------------------------
# CONTRACT — ground-truth rows
# ---------------------------------------------------------------------------


def test_ground_truth_ids_are_unique_and_ordered(run):
    ids = [r["gt"].ground_truth_id for r in run]
    assert ids == [f"GT-{i:04d}" for i in range(1, 11)]


def test_ground_truth_echoes_the_placement(run):
    for r in run:
        gt = r["gt"]
        assert gt.incident_type == r["kind"].value
        assert gt.injected_at == r["start"]
        assert gt.duration_hours == r["duration"]
        assert gt.chronological_split == r["split"]
        assert gt.simulator_seed == SEED
        assert gt.organization_id == ORGANIZATION_ID
        assert gt.expected_safe_remediation == INCIDENT_SPECS[r["kind"]].safe_remediation
        assert gt.resolved_incident_id is None  # Phase 2+ fills this in


def test_expected_magnitude_has_exactly_its_types_keys(run):
    """Convention 15. Three of the four are MEASURED outcomes, not the input
    parameter - so a missing key here is a magnitude that silently reads as
    absent to evaluation code."""
    for r in run:
        assert set(r["gt"].expected_magnitude) == EXPECTED_MAGNITUDE_KEYS[r["kind"]]
        assert all(
            isinstance(v, float) and np.isfinite(v) for v in r["gt"].expected_magnitude.values()
        )


def test_logging_regression_records_its_input_parameter(run):
    """The one type whose magnitude is copied rather than measured."""
    for r in _records(run, IncidentType.LOGGING_REGRESSION):
        assert r["gt"].expected_magnitude["log_byte_multiplier"] == pytest.approx(r["magnitude"])


def test_measured_magnitudes_are_positive(run):
    for r in run:
        if r["kind"] is IncidentType.LOGGING_REGRESSION:
            continue
        assert all(v > 0 for v in r["gt"].expected_magnitude.values()), r["kind"]


@pytest.mark.parametrize("kind", sorted(IncidentType, key=lambda k: k.value))
def test_expected_magnitude_raises_when_a_measurement_is_missing(kind):
    """A loud failure beats a ground-truth row with a silently absent value."""
    spec = INCIDENT_SPECS[kind]
    if kind is IncidentType.LOGGING_REGRESSION:
        assert spec.expected_magnitude(4.0)  # needs no measurement
        return
    with pytest.raises(KeyError):
        spec.expected_magnitude(4.0)


def test_affected_target_is_a_resource_for_idle_and_a_service_otherwise(run, clean):
    """The spec names a RESOURCE for idle_accelerator and a SERVICE for the
    other three, because that is the grain each one is actually detectable at."""
    for r in run:
        target = r["gt"].affected_service_or_resource
        if r["kind"] is IncidentType.IDLE_ACCELERATOR:
            key = (r["service"].project, r["service"].name)
            assert target in set(clean[key][1]["resource_id"].unique())
        else:
            assert target == r["service"].name


# ---------------------------------------------------------------------------
# DISTRIBUTION — materiality
# ---------------------------------------------------------------------------
# docs/phase0/threat-model-governance.md: unexplained cost > $50/day OR > 5% of
# expected cost. An injected incident below both bars is ground truth a detector
# is correct to ignore - a mislabelled row rather than a hard case.


def _materiality(org, hours, bounds, seeds):
    """Mean daily cost delta per incident, and the org-wide daily total."""
    out: dict[IncidentType, list[float]] = {}
    org_daily = []
    for seed in seeds:
        clean = _clean_run(org, hours, seed)
        org_daily.append(sum(b["effective_cost"].sum() for _, _, b in clean.values()) / RUN_DAYS)
        for r in _inject_all(org, hours, bounds, clean, seed):
            rows = _in_window(r["clean_billing"], r["start"], r["duration"])
            delta = (
                r["billing"].loc[rows, "effective_cost"].sum()
                - r["clean_billing"].loc[rows, "effective_cost"].sum()
            ) / (r["duration"] / 24.0)
            out.setdefault(r["kind"], []).append(delta)
    return out, float(np.mean(org_daily))


@pytest.fixture(scope="module")
def materiality(org, hours, bounds):
    return _materiality(org, hours, bounds, range(SEEDS_FOR_DISTRIBUTION))


@pytest.mark.parametrize(
    "kind",
    [
        IncidentType.LOGGING_REGRESSION,
        IncidentType.QUERY_REGRESSION,
        IncidentType.IDLE_ACCELERATOR,
    ],
)
def test_incident_types_clear_the_materiality_bar_on_every_seed(materiality, kind):
    deltas, org_daily = materiality
    bar = min(MATERIALITY_USD_PER_DAY, org_daily * MATERIALITY_ORG_SHARE)
    worst = min(deltas[kind])
    assert worst > bar, (
        f"{kind.value} weakest injection is ${worst:.1f}/day against a ${bar:.1f}/day bar"
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "KNOWN GAP, not a flake. autoscaling_error falls below the materiality bar on "
        "roughly half of all injections across the full 3-5x band. capacity.py applies "
        "min_replicas=4 as a single global floor while regions carry 50/30/20 of traffic, "
        "so a 3x floor of 12 sits far below us-central1's trough of ~7 instances and lifts "
        "only ~7% of its hours - and us-central1 is half the spend. The fix is a "
        "region-aware replica floor in capacity.py; see docs/phase1/decisions.md. Until "
        "then these rows are ground truth a detector is right to ignore."
    ),
)
def test_autoscaling_error_clears_the_materiality_bar_on_every_seed(materiality):
    deltas, org_daily = materiality
    bar = min(MATERIALITY_USD_PER_DAY, org_daily * MATERIALITY_ORG_SHARE)
    assert min(deltas[IncidentType.AUTOSCALING_ERROR]) > bar


def test_incidents_stay_an_exceptional_state_on_every_service(run, hours):
    """Measured PER SERVICE, which is the grain a detector calibrates at - two
    incidents on different services at the same time do not make either service
    more incident-ridden. If a service spent most of the 90 days under an
    incident, Phase 2 would learn its "normal" baseline from incident data."""
    covered: dict[str, set] = {}
    for r in run:
        covered.setdefault(r["service"].name, set()).update(
            pd.date_range(r["start"], periods=r["duration"], freq="h", tz="UTC")
        )
    assert covered, "no incidents placed"
    for name, span in covered.items():
        assert len(span) / len(hours) < 0.30, (name, len(span))
    assert sum(len(s) for s in covered.values()) / len(hours) > 0.05