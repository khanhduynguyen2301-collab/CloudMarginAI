"""Tests for simulator/labels/ground_truth.py.

Mostly built from hand-made rows - this module is serialization, and it should
not need a 90-day run to be exercised. One integration test at the bottom does
use real engine output, because the thing it checks (that no numpy scalar
reaches the writer) is only true if engine.py's convention 15 casts are all
actually there.

Three properties carry the weight.

Separation (convention 18). The file must be unreachable by a wildcard over the
run directory. This is the only mechanical guard against Phase 2 or 3 training
on its own answer key, and a comment saying "don't read this" is not a guard.

Determinism. Byte-identical reruns are Phase 1's exit gate, and this file is
part of the run output - so the same rows must produce the same bytes, on any
machine, in any directory.

Leak containment (convention 20). manifest.json is a file feature-building code
may legitimately read. If a magnitude ever appears in it, the answer key exists
in two places and one of them is not protected.
"""

from __future__ import annotations

import json
from dataclasses import fields
from datetime import datetime
from typing import Union, get_args, get_origin, get_type_hints

import numpy as np
import pandas as pd
import pytest

from simulator.cost_model.billing import usage_to_billing_rows
from simulator.incidents.engine import inject, place_incidents
from simulator.incidents.types import INCIDENT_SPECS
from simulator.labels.ground_truth import (
    _TIMESTAMP_FIELDS,
    GROUND_TRUTH_DIRNAME,
    GROUND_TRUTH_FILENAME,
    ground_truth_path,
    manifest_entries,
    read_ground_truth,
    write_ground_truth,
)
from simulator.schema import GroundTruthIncidentRow
from simulator.seeding import rng_for
from simulator.topology import ORGANIZATION_ID, build_topology
from simulator.workloads.capacity import generate_capacity_and_reliability
from simulator.workloads.demand import generate_demand

SEED = 42


def _row(ordinal: int = 1, **overrides) -> GroundTruthIncidentRow:
    """A syntactically valid ground-truth row, cheap to build."""
    defaults = dict(
        ground_truth_id=f"GT-{ordinal:04d}",
        organization_id=ORGANIZATION_ID,
        incident_type="logging_regression",
        affected_service_or_resource="ingestion-worker",
        injected_at=pd.Timestamp("2026-01-19T22:00:00", tz="UTC"),
        duration_hours=164,
        expected_magnitude={"log_byte_multiplier": 3.9600844205803947},
        expected_safe_remediation="Restore logging level",
        chronological_split="train",
        simulator_seed=SEED,
    )
    defaults.update(overrides)
    return GroundTruthIncidentRow(**defaults)


@pytest.fixture
def rows() -> list[GroundTruthIncidentRow]:
    return [
        _row(1),
        _row(
            2,
            incident_type="query_regression",
            affected_service_or_resource="checkout-api",
            expected_magnitude={"latency_delta_pct": 83.3, "cost_delta_pct": 105.2},
            expected_safe_remediation="Rollback query change",
            duration_hours=80,
        ),
        _row(
            3,
            incident_type="idle_accelerator",
            affected_service_or_resource="prod-ml-training-job-us-central1-001",
            expected_magnitude={"idle_cost_rate": 13.879203101378039},
            expected_safe_remediation="Stop or schedule the resource",
            chronological_split="test",
            duration_hours=194,
        ),
    ]


# ---------------------------------------------------------------------------
# CONTRACT — separation from the analytical tables (convention 18)
# ---------------------------------------------------------------------------


def test_path_is_inside_a_dedicated_subdirectory(tmp_path):
    path = ground_truth_path(tmp_path)
    assert path.parent.name == GROUND_TRUTH_DIRNAME
    assert path.name == GROUND_TRUTH_FILENAME
    assert path.parent.parent == tmp_path


def test_a_wildcard_over_the_run_directory_cannot_reach_it(tmp_path, rows):
    """The actual guard. A detector globbing its inputs out of the run directory
    must not pick up the answer key - which is a property of where the file
    sits, not of anyone remembering to skip it."""
    write_ground_truth(rows, tmp_path)
    (tmp_path / "billing_hourly.parquet").write_bytes(b"stub")
    (tmp_path / "resource_metrics_hourly.parquet").write_bytes(b"stub")

    top_level_files = [p for p in tmp_path.glob("*") if p.is_file()]
    assert all(p.suffix == ".parquet" for p in top_level_files)
    assert not list(tmp_path.glob("*.jsonl"))
    assert not list(tmp_path.glob("*ground_truth*.parquet"))


def test_write_creates_the_subdirectory(tmp_path, rows):
    target = tmp_path / "runs" / "seed-42"
    path = write_ground_truth(rows, target)
    assert path.exists()
    assert path == ground_truth_path(target)


# ---------------------------------------------------------------------------
# CONTRACT — determinism
# ---------------------------------------------------------------------------


def test_same_rows_produce_identical_bytes_in_different_directories(tmp_path, rows):
    """Part of the byte-identical rerun gate. The output must not depend on the
    path it is written to, the machine, or anything ambient."""
    a = write_ground_truth(rows, tmp_path / "a")
    b = write_ground_truth(rows, tmp_path / "b")
    assert a.read_bytes() == b.read_bytes()


def test_rewriting_replaces_rather_than_appends(tmp_path, rows):
    """Re-running a seed into the same directory must not double the table."""
    write_ground_truth(rows, tmp_path)
    path = write_ground_truth(rows, tmp_path)
    assert len(path.read_text().splitlines()) == len(rows)


def test_line_endings_are_lf_on_every_platform(tmp_path, rows):
    """Windows would otherwise write CRLF, and the same seed would hash
    differently on a Windows machine than on CI."""
    raw = write_ground_truth(rows, tmp_path).read_bytes()
    assert b"\r" not in raw
    assert raw.endswith(b"\n")


def test_one_line_per_row_in_the_order_given(tmp_path, rows):
    lines = write_ground_truth(rows, tmp_path).read_text().splitlines()
    assert len(lines) == len(rows)
    assert [json.loads(line)["ground_truth_id"] for line in lines] == [
        r.ground_truth_id for r in rows
    ]


def test_fields_are_written_in_dataclass_order(tmp_path, rows):
    """Key order is part of the bytes. Deriving it from the dataclass means it
    cannot drift with dict-construction order."""
    first = json.loads(write_ground_truth(rows, tmp_path).read_text().splitlines()[0])
    assert list(first) == [f.name for f in fields(GroundTruthIncidentRow)]


# ---------------------------------------------------------------------------
# CONTRACT — round trip
# ---------------------------------------------------------------------------


def test_round_trip_is_exact(tmp_path, rows):
    write_ground_truth(rows, tmp_path)
    assert read_ground_truth(tmp_path) == rows


def test_round_trip_survives_a_second_write(tmp_path, rows):
    """write -> read -> write must be a fixed point, or two runs of the same
    seed could differ purely by having been reloaded in between."""
    first = write_ground_truth(rows, tmp_path / "a").read_bytes()
    again = write_ground_truth(read_ground_truth(tmp_path / "a"), tmp_path / "b")
    assert again.read_bytes() == first


def test_timestamps_round_trip_as_utc(tmp_path, rows):
    write_ground_truth(rows, tmp_path)
    record = json.loads(ground_truth_path(tmp_path).read_text().splitlines()[0])
    assert record["injected_at"].endswith("+00:00")
    assert read_ground_truth(tmp_path)[0].injected_at == rows[0].injected_at


def test_magnitude_payloads_keep_their_per_type_shape(tmp_path, rows):
    """Convention 19's reason for existing: three different key sets in one
    table, which is what a columnar format would have flattened."""
    write_ground_truth(rows, tmp_path)
    shapes = [set(r.expected_magnitude) for r in read_ground_truth(tmp_path)]
    assert shapes == [set(r.expected_magnitude) for r in rows]
    assert len({frozenset(s) for s in shapes}) == 3


def test_optional_fields_round_trip_as_none(tmp_path, rows):
    write_ground_truth(rows, tmp_path)
    assert all(r.resolved_incident_id is None for r in read_ground_truth(tmp_path))


def test_blank_lines_are_tolerated_on_read(tmp_path, rows):
    path = write_ground_truth(rows, tmp_path)
    path.write_text(path.read_text() + "\n\n")
    assert len(read_ground_truth(tmp_path)) == len(rows)


# ---------------------------------------------------------------------------
# CONTRACT — refusals
# ---------------------------------------------------------------------------


def test_empty_rows_is_refused(tmp_path):
    """An empty table would satisfy a downstream join silently, and a run with
    no incidents cannot meet the M1 exit criteria anyway."""
    with pytest.raises(ValueError, match="empty"):
        write_ground_truth([], tmp_path)
    assert not ground_truth_path(tmp_path).exists()


def test_duplicate_ground_truth_ids_are_refused(tmp_path, rows):
    """A duplicated id makes one incident score twice and another disappear."""
    with pytest.raises(ValueError, match="GT-0001"):
        write_ground_truth([rows[0], rows[0]], tmp_path)


def test_numpy_scalars_are_coerced_not_rejected(tmp_path):
    """Magnitudes are measured off numpy arrays (convention 15). engine.py casts
    them, but the writer accepts a numpy scalar rather than failing a whole run
    over one missed cast - and what lands on disk is a plain JSON number."""
    row = _row(expected_magnitude={"log_byte_multiplier": np.float64(4.25)})
    path = write_ground_truth([row], tmp_path)
    value = json.loads(path.read_text())["expected_magnitude"]["log_byte_multiplier"]
    assert isinstance(value, float) and value == 4.25


def test_an_unserializable_value_names_itself(tmp_path):
    """The failure has to say which value, or it surfaces as an opaque TypeError
    on the last step of a multi-minute run."""
    row = _row(expected_magnitude={"window": pd.Timestamp("2026-01-01", tz="UTC")})
    with pytest.raises(TypeError, match="Timestamp"):
        write_ground_truth([row], tmp_path)


def test_reading_a_run_with_no_ground_truth_raises(tmp_path):
    with pytest.raises(FileNotFoundError, match="no ground truth"):
        read_ground_truth(tmp_path)


def test_reading_an_unknown_field_raises(tmp_path, rows):
    """A file written by a future schema version should fail loudly rather than
    drop a field on the floor."""
    path = write_ground_truth(rows, tmp_path)
    record = json.loads(path.read_text().splitlines()[0])
    record["remediated_at"] = "2026-01-26T00:00:00+00:00"
    path.write_text(json.dumps(record) + "\n")
    with pytest.raises(ValueError, match="remediated_at"):
        read_ground_truth(tmp_path)


def test_timestamp_fields_covers_every_datetime_in_the_schema():
    """Drift guard. _TIMESTAMP_FIELDS is hardcoded; this fails the moment
    GroundTruthIncidentRow gains a datetime that is not listed - which would
    otherwise only surface as a TypeError during a real run, with a message
    pointing at the wrong fix."""
    expected = set()
    for name, hint in get_type_hints(GroundTruthIncidentRow).items():
        candidates = get_args(hint) if get_origin(hint) is Union else (hint,)
        if any(isinstance(c, type) and issubclass(c, datetime) for c in candidates):
            expected.add(name)
    assert set(_TIMESTAMP_FIELDS) == expected


# ---------------------------------------------------------------------------
# CONTRACT — manifest leak containment (convention 20)
# ---------------------------------------------------------------------------


def test_manifest_entries_carry_no_magnitude(rows):
    """The rule the whole convention exists for. manifest.json is a file
    feature-building code may legitimately read."""
    entries = manifest_entries(rows)
    blob = json.dumps(entries)
    assert "magnitude" not in blob
    for row in rows:
        for value in row.expected_magnitude.values():
            assert str(value) not in blob


def test_manifest_entries_keep_what_is_already_knowable(rows):
    """Everything retained can be read off the data itself: that an incident
    happened, where, and when. Only the answer key is withheld."""
    entries = manifest_entries(rows)
    assert len(entries) == len(rows)
    for entry, row in zip(entries, rows):
        assert entry["ground_truth_id"] == row.ground_truth_id
        assert entry["incident_type"] == row.incident_type
        assert entry["affected_service_or_resource"] == row.affected_service_or_resource
        assert entry["duration_hours"] == row.duration_hours
        assert entry["chronological_split"] == row.chronological_split
        assert pd.Timestamp(entry["injected_at"]) == row.injected_at


def test_manifest_entries_are_json_serializable(rows):
    """They go straight into manifest.json, so a numpy scalar here would fail
    the run at the very last step."""
    assert json.loads(json.dumps(manifest_entries(rows))) == manifest_entries(rows)


# ---------------------------------------------------------------------------
# INTEGRATION — real engine output
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def real_rows():
    """One full run's ground truth, from the real generators."""
    hours = pd.date_range("2026-01-01", periods=24 * 90, freq="h", tz="UTC")
    org = build_topology()
    bounds = {
        "train": (hours[0], hours[0] + pd.Timedelta(days=60)),
        "validation": (hours[0] + pd.Timedelta(days=60), hours[0] + pd.Timedelta(days=75)),
        "test": (hours[0] + pd.Timedelta(days=75), hours[0] + pd.Timedelta(days=90)),
    }
    clean = {}
    for name in sorted({spec.service_name for spec in INCIDENT_SPECS.values()}):
        service = org.get_service(name)
        d = generate_demand(
            service, hours, rng_for(SEED, f"workload:prod:{name}"), run_start=hours[0]
        )
        m = generate_capacity_and_reliability(service, d, rng_for(SEED, f"capacity:prod:{name}"))
        b = usage_to_billing_rows(
            service, d, m, rng_for(SEED, f"cost_noise:prod:{name}"), run_start=hours[0]
        )
        clean[name] = (d, m, b)

    rng = rng_for(SEED, "incidents")
    out = []
    for ordinal, (kind, service, start, duration, magnitude) in enumerate(
        place_incidents(org, hours, bounds, rng), start=1
    ):
        d, m, b = clean[service.name]
        split = next(n for n, (a, z) in bounds.items() if a <= start < z)
        *_, gt = inject(
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
            simulator_seed=SEED,
            ordinal=ordinal,
        )
        out.append(gt)
    return out


def test_a_real_run_writes_ten_rows_and_reads_back_identically(tmp_path, real_rows):
    assert len(real_rows) == 10
    write_ground_truth(real_rows, tmp_path)
    assert read_ground_truth(tmp_path) == real_rows


def test_real_magnitudes_reach_the_writer_as_plain_floats(real_rows):
    """Really a test of engine.py's convention 15 casts. Measured magnitudes come
    off numpy arrays, and a missed float() would only show up here."""
    for row in real_rows:
        for key, value in row.expected_magnitude.items():
            assert type(value) is float, (row.ground_truth_id, key, type(value))


def test_a_real_run_covers_every_incident_type_and_split(real_rows):
    assert len({r.incident_type for r in real_rows}) == 4
    assert {r.chronological_split for r in real_rows} == {"train", "validation", "test"}