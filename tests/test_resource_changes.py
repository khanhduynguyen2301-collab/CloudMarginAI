"""Tests for simulator/changes/resource_changes.py.

Same split as test_deployments.py: CONTRACT is single-seed safe, DISTRIBUTION
aggregates across seeds because the per-seed count is Poisson.

Two properties here matter beyond correctness. Every row must join to a real
resource_id from capacity.py, because that join is how Phase 3 connects a
candidate change to the resource whose metrics moved. And (resource_id,
changed_at) must be unique: the frozen schema gives resource_changes no
change_id, so that pair is the only thing a piece of evidence can cite.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from simulator.changes.resource_changes import (
    BENIGN_CHANGE_TYPES,
    CHANGE_ACTORS,
    MEAN_DAYS_BETWEEN_CHANGES,
    _config_pair,
    generate_resource_changes,
)
from simulator.incidents.engine import CAUSAL_EVENT_TAG
from simulator.seeding import rng_for
from simulator.topology import ORGANIZATION_ID, build_topology
from simulator.workloads.capacity import generate_capacity_and_reliability, resource_id_for
from simulator.workloads.capacity import params_for as capacity_params_for
from simulator.workloads.demand import generate_demand

RUN_START = pd.Timestamp("2026-01-01", tz="UTC")
RUN_DAYS = 90
SEEDS_FOR_DISTRIBUTION = 200

RESERVED_VALUES = {value for _, value in CAUSAL_EVENT_TAG.values()}

# Which config key each benign change_type is expected to edit.
EXPECTED_CONFIG_KEY = {
    "update_label": "labels",
    "update_tag": "tags",
    "update_description": "description",
    "update_budget_alert": "budget_alert_usd",
    "rotate_service_account_key": "key_version",
}


@pytest.fixture(scope="module")
def hours() -> pd.DatetimeIndex:
    return pd.date_range(RUN_START, periods=24 * RUN_DAYS, freq="h", tz="UTC")


@pytest.fixture(scope="module")
def org():
    return build_topology()


def _changes(service, hours, seed: int = 42):
    return generate_resource_changes(
        service, hours, rng_for(seed, f"changes:{service.project}:{service.name}")
    )


@pytest.fixture(scope="module")
def all_changes(org, hours):
    return {(s.project, s.name): _changes(s, hours) for s in org.all_services()}


# ---------------------------------------------------------------------------
# CONTRACT — convention 12
# ---------------------------------------------------------------------------


def test_reserved_vocabulary_never_appears(all_changes):
    """Convention 12. "schedule_removed" and "update_scaling_policy" mark the
    config change that CAUSED an incident. If background noise emitted them,
    Phase 3's ranker could find a true cause by string match."""
    for key, rows in all_changes.items():
        emitted = {row.change_type for row in rows}
        assert not (emitted & RESERVED_VALUES), (key, emitted & RESERVED_VALUES)


def test_change_type_comes_from_the_benign_set(all_changes):
    for key, rows in all_changes.items():
        assert {row.change_type for row in rows} <= set(BENIGN_CHANGE_TYPES), key


def test_benign_and_reserved_vocabularies_do_not_overlap():
    """A guard on the constants themselves, independent of any generated run."""
    assert not (set(BENIGN_CHANGE_TYPES) & RESERVED_VALUES)


# ---------------------------------------------------------------------------
# CONTRACT — coverage, and the join Phase 3 depends on
# ---------------------------------------------------------------------------


def test_every_service_gets_changes_including_staging(all_changes):
    """Unlike deployments, resource_changes carries project_id — which is what
    gives staging any change activity at all."""
    for key, rows in all_changes.items():
        assert rows, key
    assert any(project != "prod" for project, _ in all_changes)


def test_resource_ids_join_to_real_capacity_resources(org, hours, all_changes):
    """The join that lets Phase 3 connect a candidate change to the resource
    whose metrics actually moved. An orphaned id breaks that silently."""
    real: set[str] = set()
    for service in org.all_services():
        demand = generate_demand(
            service,
            hours,
            rng_for(42, f"workload:{service.project}:{service.name}"),
            run_start=hours[0],
        )
        metrics = generate_capacity_and_reliability(
            service, demand, rng_for(42, f"capacity:{service.project}:{service.name}")
        )
        real |= set(metrics.resource_id.unique())

    for key, rows in all_changes.items():
        orphans = {row.resource_id for row in rows} - real
        assert not orphans, (key, orphans)


def test_resource_ids_belong_to_this_service_only(org, all_changes):
    for service in org.all_services():
        rows = all_changes[(service.project, service.name)]
        expected = {
            resource_id_for(service, region)
            for region in capacity_params_for(service).region_weights
        }
        assert {row.resource_id for row in rows} <= expected, service.name


def test_resource_id_and_changed_at_form_a_unique_key(all_changes):
    """resource_changes has no change_id in the frozen schema, so this pair is
    the only handle a piece of evidence can cite. If it were not unique, two
    distinct changes would be indistinguishable to the ranker."""
    combined = [(row.resource_id, row.changed_at) for rows in all_changes.values() for row in rows]
    assert len(set(combined)) == len(combined)


# ---------------------------------------------------------------------------
# CONTRACT — config payloads
# ---------------------------------------------------------------------------


def test_both_config_sides_are_populated(all_changes):
    """A consumer should never have to guess what a null means."""
    for key, rows in all_changes.items():
        for row in rows:
            assert row.before_config, (key, row.change_type)
            assert row.after_config, (key, row.change_type)


def test_no_change_is_a_no_op(all_changes):
    for key, rows in all_changes.items():
        for row in rows:
            assert row.before_config != row.after_config, (key, row.change_type)


def test_config_shape_matches_the_change_type(all_changes):
    for key, rows in all_changes.items():
        for row in rows:
            expected_key = EXPECTED_CONFIG_KEY[row.change_type]
            assert expected_key in row.before_config, (key, row.change_type)
            assert expected_key in row.after_config, (key, row.change_type)


@pytest.mark.parametrize("change_type", sorted(BENIGN_CHANGE_TYPES))
def test_config_pair_is_defined_for_every_benign_type(change_type):
    """Adding a change_type without a config shape would raise mid-run; this
    catches it at test time instead."""
    before, after = _config_pair(change_type, rng_for(42, "x"))
    assert before and after and before != after


def test_config_pair_rejects_an_unknown_change_type():
    with pytest.raises(ValueError, match="no config shape"):
        _config_pair("not-a-real-change", rng_for(42, "x"))


def test_configs_are_json_serializable(all_changes):
    """before_config/after_config are JSON columns in the frozen schema."""
    import json

    for key, rows in all_changes.items():
        for row in rows:
            json.dumps(row.before_config)
            json.dumps(row.after_config)


# ---------------------------------------------------------------------------
# CONTRACT — row structure, reproducibility, edges
# ---------------------------------------------------------------------------


def test_rows_are_ordered_by_change_time(all_changes):
    for key, rows in all_changes.items():
        times = [row.changed_at for row in rows]
        assert times == sorted(times), key


def test_actors_come_from_the_roster(all_changes):
    for key, rows in all_changes.items():
        assert {row.actor for row in rows} <= set(CHANGE_ACTORS), key


def test_fixed_columns(org, all_changes):
    for service in org.all_services():
        rows = all_changes[(service.project, service.name)]
        assert {row.organization_id for row in rows} == {ORGANIZATION_ID}, service.name
        assert {row.project_id for row in rows} == {service.project}, service.name
        assert {row.schema_version for row in rows} == {"1"}, service.name


def test_ingested_at_is_the_change_time_not_the_wall_clock(all_changes):
    """Convention 11. A datetime.now() here breaks the byte-identical rerun."""
    for key, rows in all_changes.items():
        assert all(row.ingested_at == row.changed_at for row in rows), key


def test_changes_land_on_run_hours(all_changes, hours):
    valid = set(hours)
    for key, rows in all_changes.items():
        assert all(row.changed_at in valid for row in rows), key


def test_same_seed_reproduces_and_different_seed_does_not(org, hours):
    service = org.get_service("web-frontend")
    assert _changes(service, hours, 42) == _changes(service, hours, 42)
    assert _changes(service, hours, 42) != _changes(service, hours, 7)


def test_empty_hours_yields_no_rows(org):
    empty = pd.DatetimeIndex([], dtype="datetime64[ns, UTC]")
    assert _changes(org.get_service("web-frontend"), empty) == []


# ---------------------------------------------------------------------------
# DISTRIBUTION — aggregated across seeds
# ---------------------------------------------------------------------------
# Per-seed count is Poisson(days / MEAN_DAYS_BETWEEN_CHANGES * n_resources).
# For a three-region prod service that expectation is 13.5, sd ~= 3.7; over 200
# seeds the standard error is ~0.26, so a 10% tolerance is roughly 5 SE.


@pytest.mark.parametrize(
    "project,service_name",
    [("prod", "web-frontend"), ("prod", "ml-training-job"), ("staging", "checkout-api")],
)
def test_change_rate_scales_with_resource_count(org, hours, project, service_name):
    """The rate is per-resource, so a single-region service (ml-training-job,
    staging) should see a third of a three-region service's changes."""
    service = org.get_service(service_name, project=project)
    n_resources = len(capacity_params_for(service).region_weights)
    counts = [len(_changes(service, hours, seed)) for seed in range(SEEDS_FOR_DISTRIBUTION)]
    expected = RUN_DAYS / MEAN_DAYS_BETWEEN_CHANGES * n_resources
    assert float(np.mean(counts)) == pytest.approx(expected, rel=0.10)


def test_changes_are_not_clustered_in_business_hours(org, hours):
    """Deliberate contrast with deployments.py, where releases cluster at ~80%
    in business hours. Config changes are half automated housekeeping, so they
    land uniformly — which means an incident at 03:00 has plausible config
    candidates even when it has no plausible deploy candidates."""
    service = org.get_service("web-frontend")
    rows = [r for seed in range(30) for r in _changes(service, hours, seed)]
    in_business = sum(1 for r in rows if 9 <= r.changed_at.hour < 18)
    # Uniform over 24h would put 9/24 = 37.5% in the window.
    assert 0.28 < in_business / len(rows) < 0.48


def test_changes_spread_across_a_services_regions(org, hours):
    """resource_id is drawn uniformly, so a three-region service should not
    concentrate its changes on one resource."""
    service = org.get_service("web-frontend")
    rows = [r for seed in range(30) for r in _changes(service, hours, seed)]
    share = pd.Series([r.resource_id for r in rows]).value_counts(normalize=True)
    assert len(share) == 3
    assert share.max() < 0.45


def test_enough_benign_changes_that_proximity_alone_is_a_poor_ranker(org, hours):
    """incident-injection-spec.md injects 10 incidents per run. Two of the four
    incident types are caused by resource_changes, so the benign pool has to be
    large enough that "a config change happened nearby" is not a free answer."""
    total = sum(len(_changes(s, hours)) for s in org.all_services())
    assert total / 10 >= 5.0, f"only {total} benign changes against 10 incidents"
