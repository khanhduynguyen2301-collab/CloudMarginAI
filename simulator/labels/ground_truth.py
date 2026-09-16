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
