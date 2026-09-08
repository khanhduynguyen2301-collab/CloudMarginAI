"""Persist ground_truth_incidents rows for a run.

Per docs/phase1/incident-injection-spec.md: this table is read only by
evaluation code (Phase 2's backtest harness, Phase 3's ranker scorer) — it
must never be joined into features_hourly or any table a detector reads,
or the evaluation becomes circular. Keep its output file/table physically
separate from the five analytical tables for that reason (e.g. a
dedicated `ground_truth_incidents.parquet`, not bundled into the same
directory a detector would glob over).
"""
from __future__ import annotations

from pathlib import Path

from simulator.schema import GroundTruthIncidentRow


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
