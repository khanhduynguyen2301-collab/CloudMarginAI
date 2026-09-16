"""Persist ground_truth_incidents rows for a run.

Per docs/phase1/incident-injection-spec.md: this table is read only by
evaluation code (Phase 2's backtest harness, Phase 3's ranker scorer) - it must
never be joined into features_hourly or any table a detector reads, or the
evaluation becomes circular.

Three conventions this module fixes, numbered on from engine.py's:

18. GROUND TRUTH LIVES IN ITS OWN SUBDIRECTORY, never beside the analytical
    tables. A detector that globs the run directory for its inputs must not be
    able to reach this file by accident - and "we will remember not to read it"
    is not a control, it is a hope. The five analytical tables go in
    output_dir/; ground truth goes in output_dir/ground_truth/. Anything that
    reads the former with a wildcard now structurally cannot pick up the latter.

19. JSON LINES, NOT PARQUET. `expected_magnitude` carries different keys per
    incident type (convention 15), which a columnar schema can only express as
    a union struct full of nulls or as an opaque JSON string - both worse than
    just writing JSON. The table is ~10 rows, so there is no performance
    argument either way, and what does matter is that a human auditing an
    evaluation can read it without a parquet reader.

20. THE MANIFEST NEVER CARRIES MAGNITUDE. simulator-architecture.md's run
    manifest lists injected incidents by ID *without* their ground-truth
    magnitude, so the magnitude exists in exactly one file. `manifest_entries`
    below is the only thing cli.py should call to build that list, which keeps
    the rule enforced in one place instead of remembered at the call site.

Determinism note: byte-identical reruns are Phase 1's exit gate, so this writer
emits fields in dataclass order, timestamps as ISO-8601 UTC, and nothing that
depends on the wall clock or on dict ordering. The same seed produces the same
bytes.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import fields
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from simulator.schema import GroundTruthIncidentRow

# Convention 18: a subdirectory, not a sibling of the analytical tables.
GROUND_TRUTH_DIRNAME = "ground_truth"
GROUND_TRUTH_FILENAME = "ground_truth_incidents.jsonl"

_TIMESTAMP_FIELDS = ("injected_at",)


def ground_truth_path(output_dir: Path) -> Path:
    """Where this run's ground truth lives, relative to its output directory."""
    return Path(output_dir) / GROUND_TRUTH_DIRNAME / GROUND_TRUTH_FILENAME


def _jsonable(value: Any) -> Any:
    """Coerce a numpy scalar to its Python equivalent, and refuse anything else.

    engine.py already casts its measured magnitudes to float, but they are
    computed from numpy arrays - so one missed cast anywhere upstream would
    otherwise surface as a TypeError deep inside json.dumps, on the last step of
    a multi-minute run. Failing here says which value and why.
    """
    if isinstance(value, np.generic):
        return value.item()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    raise TypeError(
        f"ground_truth_incidents cannot serialize {type(value).__name__} ({value!r}) - "
        "cast it to a plain Python type where it is produced, not here"
    )


def _to_record(row: GroundTruthIncidentRow) -> dict:
    """One row as a JSON-ready dict, fields in dataclass order (determinism)."""
    record: dict[str, Any] = {}
    for spec in fields(row):
        value = getattr(row, spec.name)
        if spec.name in _TIMESTAMP_FIELDS:
            record[spec.name] = pd.Timestamp(value).isoformat()
        else:
            record[spec.name] = _jsonable(value)
    return record


def _from_record(record: dict) -> GroundTruthIncidentRow:
    known = {spec.name for spec in fields(GroundTruthIncidentRow)}
    unknown = set(record) - known
    if unknown:
        raise ValueError(
            f"ground truth row has fields not in GroundTruthIncidentRow: {sorted(unknown)} - "
            "the file was written by a different schema version"
        )
    payload = dict(record)
    for name in _TIMESTAMP_FIELDS:
        payload[name] = pd.Timestamp(payload[name])
    return GroundTruthIncidentRow(**payload)


def write_ground_truth(rows: list[GroundTruthIncidentRow], output_dir: Path) -> Path:
    """Write all ground_truth_incidents rows for a run to `output_dir`.

    Args:
        rows: one GroundTruthIncidentRow per injected incident (should be
            exactly 10 for a full run, per incident-injection-spec.md).
        output_dir: the run's output directory (same one manifest.json
            goes into — see simulator/cli.py).

    Returns:
        The path written to.
    """
    raise NotImplementedError("TODO: serialize rows (e.g. to parquet or JSON lines)")
