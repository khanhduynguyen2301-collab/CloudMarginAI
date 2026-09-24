"""Tests for simulator/changes/deployments.py.

Two groups:

  * CONTRACT     — structure, conventions, reproducibility. Single-seed safe.
  * DISTRIBUTION — rate and timing, which only hold in expectation. These
                   aggregate across many seeds rather than asserting on one:
                   the per-seed count is Poisson with sd ~= sqrt(expected), so
                   a single draw sits 2-3 sd from the mean often enough that a
                   one-seed assertion would flake. Seed 42 happens to draw high
                   (web-frontend: 25 against a 400-seed mean of 14.74).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from simulator.changes.deployments import (
    CHANGED_COMPONENTS,
    DEFAULT_MEAN_DAYS_BETWEEN_RELEASES,
    MEAN_DAYS_BETWEEN_RELEASES,
    RELEASE_ACTORS,
    generate_deployments,
    mean_days_for,
)
from simulator.incidents.engine import CAUSAL_EVENT_TAG
from simulator.seeding import rng_for
from simulator.topology import ORGANIZATION_ID, build_topology

RUN_START = pd.Timestamp("2026-01-01", tz="UTC")
RUN_DAYS = 90
SEEDS_FOR_DISTRIBUTION = 200

# Values only simulator/incidents/engine.py may write, for either table.
RESERVED_VALUES = {value for _, value in CAUSAL_EVENT_TAG.values()}


@pytest.fixture(scope="module")
def hours() -> pd.DatetimeIndex:
    return pd.date_range(RUN_START, periods=24 * RUN_DAYS, freq="h", tz="UTC")


@pytest.fixture(scope="module")
def org():
    return build_topology()


def _deploys(service, hours, seed: int = 42):
    return generate_deployments(
        service, hours, rng_for(seed, f"changes:{service.project}:{service.name}")
    )


def _prod(org):
    return [s for s in org.all_services() if s.project == "prod"]


@pytest.fixture(scope="module")
def all_deploys(org, hours):
    return {s.name: _deploys(s, hours) for s in _prod(org)}


# ---------------------------------------------------------------------------
# CONTRACT — the two conventions this module fixes
# ---------------------------------------------------------------------------


def test_reserved_vocabulary_never_appears(all_deploys):
    """Convention 12. If background noise could emit "logging-config" or
    "query-layer", Phase 3's ranker could find a true cause by string match
    instead of by evidence, and its Top-3 accuracy would mean nothing."""
    for name, rows in all_deploys.items():
        emitted = {row.changed_component for row in rows}
        assert not (emitted & RESERVED_VALUES), (name, emitted & RESERVED_VALUES)


def test_changed_component_comes_from_the_benign_set(all_deploys):
    for name, rows in all_deploys.items():
        assert {row.changed_component for row in rows} <= set(CHANGED_COMPONENTS), name


def test_staging_produces_no_deployments(org, hours):
    """Convention 13. The frozen schema has no project_id, so a staging row
    would be indistinguishable from a prod one and would give the ranker
    ambiguous candidates."""
    for service in org.all_services():
        if service.project != "prod":
            assert _deploys(service, hours) == [], service.name


def test_every_prod_service_does_deploy(all_deploys):
    """The mirror of the above: prod must not be silently empty."""
    for name, rows in all_deploys.items():
        assert rows, name


# ---------------------------------------------------------------------------
# CONTRACT — row structure
# ---------------------------------------------------------------------------


def test_rows_are_ordered_by_release_time(all_deploys):
    for name, rows in all_deploys.items():
        times = [row.released_at for row in rows]
        assert times == sorted(times), name


def test_versions_increase_monotonically(all_deploys):
    for name, rows in all_deploys.items():
        parsed = [tuple(int(p) for p in row.version.lstrip("v").split(".")) for row in rows]
        assert parsed == sorted(parsed), name
        assert len(set(parsed)) == len(parsed), f"{name} repeats a version"


def test_versions_are_well_formed(all_deploys):
    for name, rows in all_deploys.items():
        for row in rows:
            assert row.version.startswith("v"), (name, row.version)
            parts = row.version.lstrip("v").split(".")
            assert len(parts) == 3 and all(p.isdigit() for p in parts), (name, row.version)


def test_deployment_ids_are_unique_and_namespaced(all_deploys):
    seen: set[str] = set()
    for name, rows in all_deploys.items():
        ids = {row.deployment_id for row in rows}
        assert len(ids) == len(rows), name
        assert not (ids & seen), f"{name} collides with another service"
        assert all(row.deployment_id.startswith(f"dep-{name}-") for row in rows), name
        seen |= ids


def test_at_most_one_release_per_hour(all_deploys):
    """Hours are sampled without replacement."""
    for name, rows in all_deploys.items():
        times = [row.released_at for row in rows]
        assert len(set(times)) == len(times), name


def test_actors_come_from_the_roster(all_deploys):
    for name, rows in all_deploys.items():
        assert {row.actor for row in rows} <= set(RELEASE_ACTORS), name


def test_fixed_columns(all_deploys):
    for name, rows in all_deploys.items():
        assert {row.organization_id for row in rows} == {ORGANIZATION_ID}, name
        assert {row.schema_version for row in rows} == {"1"}, name
        assert {row.service for row in rows} == {name}, name


def test_ingested_at_is_the_release_time_not_the_wall_clock(all_deploys):
    """Convention 11. A datetime.now() here breaks the byte-identical rerun."""
    for name, rows in all_deploys.items():
        assert all(row.ingested_at == row.released_at for row in rows), name


def test_releases_land_on_run_hours(all_deploys, hours):
    valid = set(hours)
    for name, rows in all_deploys.items():
        assert all(row.released_at in valid for row in rows), name


# ---------------------------------------------------------------------------
# CONTRACT — reproducibility and edges
# ---------------------------------------------------------------------------


def test_same_seed_reproduces_and_different_seed_does_not(org, hours):
    service = org.get_service("web-frontend")
    assert _deploys(service, hours, 42) == _deploys(service, hours, 42)
    assert _deploys(service, hours, 42) != _deploys(service, hours, 7)


def test_empty_hours_yields_no_rows(org):
    empty = pd.DatetimeIndex([], dtype="datetime64[ns, UTC]")
    assert _deploys(org.get_service("web-frontend"), empty) == []


def test_mean_days_falls_back_for_an_unknown_service(org):
    from dataclasses import replace as dc_replace

    ghost = dc_replace(org.get_service("web-frontend"), name="not-in-the-table")
    assert mean_days_for(ghost) == DEFAULT_MEAN_DAYS_BETWEEN_RELEASES


@pytest.mark.parametrize("service_name", sorted(MEAN_DAYS_BETWEEN_RELEASES))
def test_cadence_sits_in_the_documented_band(service_name):
    """workload-cost-model.md specifies mean 1 deployment per 6-9 days."""
    assert 6.0 <= MEAN_DAYS_BETWEEN_RELEASES[service_name] <= 9.0


# ---------------------------------------------------------------------------
# DISTRIBUTION — aggregated across seeds
# ---------------------------------------------------------------------------
# Per-seed count is Poisson(days / mean_days), sd ~= sqrt(expected) ~= 3.9 for
# web-frontend. Over 200 seeds the standard error of the mean is ~0.28, so a 10%
# tolerance on a ~15 expectation is roughly 5 SE — tight enough to catch a real
# rate error, loose enough never to flake.


@pytest.mark.parametrize("service_name", sorted(MEAN_DAYS_BETWEEN_RELEASES))
def test_release_rate_matches_its_configured_cadence(org, hours, service_name):
    service = org.get_service(service_name)
    counts = [len(_deploys(service, hours, seed)) for seed in range(SEEDS_FOR_DISTRIBUTION)]
    expected = RUN_DAYS / mean_days_for(service)
    assert float(np.mean(counts)) == pytest.approx(expected, rel=0.10)


def test_releases_cluster_in_business_hours(org, hours):
    """Deploys happen during the working day. This is what makes temporal
    proximity a signal the ranker has to earn: an incident starting at 03:00 is
    much less likely to be deploy-caused than one starting at 14:00."""
    service = org.get_service("web-frontend")
    rows = [r for seed in range(20) for r in _deploys(service, hours, seed)]
    in_business = sum(1 for r in rows if 9 <= r.released_at.hour < 18)
    assert in_business / len(rows) > 0.70


def test_weekend_releases_are_rare(org, hours):
    service = org.get_service("web-frontend")
    rows = [r for seed in range(20) for r in _deploys(service, hours, seed)]
    weekend = sum(1 for r in rows if r.released_at.dayofweek >= 5)
    assert weekend / len(rows) < 0.10


def test_enough_benign_deploys_that_recency_alone_is_a_poor_ranker(org, hours):
    """incident-injection-spec.md injects 10 incidents per run. If benign
    deployments were scarce, "the most recent change" would be a free answer and
    Phase 3's Top-3 accuracy would measure nothing."""
    total = sum(len(_deploys(s, hours)) for s in _prod(org))
    assert total / 10 >= 5.0, f"only {total} benign deploys against 10 incidents"
