"""Data-quality gates, reproducibility test, and causal sanity checks.

Implements docs/phase1/validation-plan.md end to end. Every check returns
a list of human-readable failure strings (empty list = pass). `run_all_gates`
aggregates them and is what simulator/cli.py calls before writing a "done"
marker — per that doc: "no dataset without a green validation run is
usable downstream."
"""
from __future__ import annotations

from datetime import datetime

import pandas as pd

from simulator.schema import GroundTruthIncidentRow

# ---------------------------------------------------------------------------
# Data-quality gates (validation-plan.md, "Data-quality gates")
# ---------------------------------------------------------------------------


def check_schema(tables: dict[str, pd.DataFrame]) -> list[str]:
    """Every output row matches its table's field set/types in
    docs/phase0/schema-v1.md / docs/phase0/incident-catalogue.md."""
    raise NotImplementedError


def check_uniqueness(tables: dict[str, pd.DataFrame]) -> list[str]:
    """No duplicate rows on each table's declared grain (e.g.
    project-service-SKU-region-hour for billing_hourly)."""
    raise NotImplementedError


def check_nulls(tables: dict[str, pd.DataFrame]) -> list[str]:
    """No unexpected nulls outside the documented nullable fields
    (revenue, and any explicitly-nullable ground-truth fields)."""
    raise NotImplementedError


def check_cost_signs(billing: pd.DataFrame) -> list[str]:
    """usage_amount, effective_cost, credits are all >= 0."""
    raise NotImplementedError


def check_unit_consistency(billing: pd.DataFrame) -> list[str]:
    """Every SKU's usage_unit matches simulator.cost_model.pricing.SKU_PRICES;
    no mixed units within a SKU."""
    raise NotImplementedError


def check_lineage(tables: dict[str, pd.DataFrame], manifest: dict) -> list[str]:
    """Every row carries organization_id and schema_version; the run
    carries a manifest.json (seed, split boundaries, incident list)."""
    raise NotImplementedError


# ---------------------------------------------------------------------------
# Reproducibility test (validation-plan.md, "Reproducibility test")
# ---------------------------------------------------------------------------


def hash_tables(tables: dict[str, pd.DataFrame]) -> dict[str, str]:
    """Row-order-independent hash per table (generation order isn't a
    guaranteed contract) — used to compare two runs with the same seed."""
    raise NotImplementedError


# ---------------------------------------------------------------------------
# Causal sanity checks (validation-plan.md, "Causal sanity checks")
# ---------------------------------------------------------------------------


def check_causal_sanity(
    tables: dict[str, pd.DataFrame],
    ground_truth: list[GroundTruthIncidentRow],
) -> list[str]:
    """For every ground_truth_incidents row, confirm the data actually
    exhibits what that incident_type claims: log bytes rise (traffic
    flat) for logging_regression; latency + db.cpu-hour rise together for
    query_regression; GPU utilization < 5% with unchanged compute.gpu-hour
    usage for idle_accelerator; instance_count up while cpu_utilization
    falls for autoscaling_error. Also: outside any incident window,
    effective_cost reconstructs exactly from
    usage_amount * unit_price * (1 - credit_rate) per SKU."""
    raise NotImplementedError


# ---------------------------------------------------------------------------
# Chronological-integrity checks (validation-plan.md)
# ---------------------------------------------------------------------------


def check_chronological_integrity(
    ground_truth: list[GroundTruthIncidentRow],
    split_boundaries: dict[str, tuple[datetime, datetime]],
) -> list[str]:
    """Train/validation/test windows are non-overlapping and cover the
    full 90 days with no gaps. Every incident's window falls entirely
    inside one split. The test split contains at least one
    logging_regression (the M2 requirement)."""
    raise NotImplementedError


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run_all_gates(
    tables: dict[str, pd.DataFrame],
    ground_truth: list[GroundTruthIncidentRow],
    manifest: dict,
    split_boundaries: dict[str, tuple[datetime, datetime]],
) -> bool:
    """Run every gate above; return True only if all pass. simulator/cli.py
    must not write a "done" marker (or hand data to Phase 2) unless this
    returns True — see the M1 exit-gate checklist in validation-plan.md.
    """
    raise NotImplementedError
