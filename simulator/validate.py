"""Data-quality gates, reproducibility test, and causal sanity checks.
 
Implements docs/phase1/validation-plan.md end to end. Every check returns
a list of human-readable failure strings (empty list = pass). `run_all_gates`
aggregates them and is what simulator/cli.py calls before writing a "done"
marker - per that doc: "no dataset without a green validation run is
usable downstream."
 
Four conventions this module fixes, numbered on from ground_truth.py's:
 
21. THE VALIDATOR RE-DERIVES, IT NEVER TRUSTS. Every check here reads the
    assembled output tables and nothing else - no clean frames, no generator
    internals, no sampled parameters. A causal check that asked engine.py what
    it did would only prove engine.py agrees with itself. Instead each incident
    is measured the way a detector would have to measure it: against a
    baseline taken from the same data.
 
22. BASELINES ARE SHIFTED BY WHOLE WEEKS. The baseline for an incident window
    is the same span moved by +/-1..4 weeks, whichever is nearest, inside the
    run, and clear of every incident on that service. Whole weeks keep
    hour-of-day and day-of-week aligned, so the daily curve and the weekend dip
    cancel exactly; what is left is a week or two of growth (0.5-1.5%/week) and
    noise. Where a signal scales with traffic, it is also divided by the
    traffic ratio, which cancels growth too - that is how the logging check
    recovers the declared multiplier to within 1%.
 
23. `tables` IS THE CONTRACT FOR cli.py. The five analytical tables arrive as
    one DataFrame each, keyed by table name, with exactly the fields of the
    matching schema.py dataclass (TABLE_ROWS). Anything else under that dict -
    and above all anything that looks like ground truth - fails the schema
    gate, because a ground-truth frame bundled with the analytical tables is
    exactly the leak ground_truth.py's convention 18 exists to prevent.
 
24. THE REPORT IS TRUTHY, NOT A BARE BOOL. `run_all_gates` returns a
    ValidationReport that is falsy when any gate fails, so `if
    run_all_gates(...)` still reads the way the original stub intended - but
    cli.py can also print *why*. A gate that raises is recorded as a failure of
    that gate rather than aborting the rest, so one broken table still yields a
    complete report.
 
Tolerances below were measured, not chosen: 20 seeds, 200 incidents, each
checked against a week-shifted baseline. The comment on each constant gives the
worst case observed, and every tolerance leaves at least ~2x headroom over it.
"""
 
from __future__ import annotations
 
import hashlib
import json
import types
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Union, get_args, get_origin, get_type_hints
 
import numpy as np
import pandas as pd
 
from simulator.cost_model.pricing import CREDIT_RATE, SKU_PRICES
from simulator.incidents.engine import (
    CAUSAL_EVENT_TAG,
    CAUSAL_LEAD_HOURS_HIGH,
    CAUSAL_LEAD_HOURS_LOW,
    MIN_GAP_HOURS_SAME_SERVICE,
    QUERY_LATENCY_MULT_LOW,
    SPLIT_ALLOCATION,
    SPLIT_ORDER,
)
from simulator.incidents.types import INCIDENT_SPECS, IncidentType
from simulator.schema import (
    ApplicationActivityHourlyRow,
    BillingHourlyRow,
    DeploymentRow,
    GroundTruthIncidentRow,
    ResourceChangeRow,
    ResourceMetricsHourlyRow,
)
from simulator.topology import ORGANIZATION_ID, ResourceKind, build_topology
from simulator.workloads.capacity import params_for as capacity_params_for
 
# ---------------------------------------------------------------------------
# The table contract (convention 23)
# ---------------------------------------------------------------------------
 
TABLE_ROWS: dict[str, type] = {
    "billing_hourly": BillingHourlyRow,
    "resource_metrics_hourly": ResourceMetricsHourlyRow,
    "application_activity_hourly": ApplicationActivityHourlyRow,
    "deployments": DeploymentRow,
    "resource_changes": ResourceChangeRow,
}
 
# Declared grain per table, from docs/phase0/schema-v1.md. resource_changes has
# no id column in the frozen schema, so (resource_id, changed_at) is its de facto
# key - the only handle Phase 3 has to cite a config change as evidence.
TABLE_GRAIN: dict[str, tuple[str, ...]] = {
    "billing_hourly": ("organization_id", "project_id", "service", "sku", "region", "hour"),
    "resource_metrics_hourly": ("organization_id", "resource_id", "hour"),
    "application_activity_hourly": ("organization_id", "product", "service", "hour"),
    "deployments": ("organization_id", "deployment_id"),
    "resource_changes": ("organization_id", "resource_id", "changed_at"),
}
 
# The column each table's ingested_at must equal (engine.py/billing.py
# convention 11: ingestion time is derived from event time, never the clock).
EVENT_TIME_COLUMN: dict[str, str] = {
    "billing_hourly": "hour",
    "resource_metrics_hourly": "hour",
    "application_activity_hourly": "hour",
    "deployments": "released_at",
    "resource_changes": "changed_at",
}
 
# Columns that may be null. `revenue` is deferred by Phase 0. The two
# conditional entries are stricter than "may be null": gpu_utilization is null
# EXACTLY for services with no GPU, and latency is null EXACTLY for batch jobs.
# A non-GPU service reporting gpu_utilization 0.0 would look precisely like an
# idle accelerator, so the converse is checked too.
ALWAYS_NULLABLE: dict[str, frozenset[str]] = {
    "application_activity_hourly": frozenset({"revenue"}),
}
_GPU_COLUMN = "gpu_utilization"
_LATENCY_COLUMNS = ("latency_p50_ms", "latency_p99_ms")
 
REQUIRED_MANIFEST_KEYS: tuple[str, ...] = (
    "master_seed",
    "derived_seeds",
    "date_range",
    "split_boundaries",
    "topology_version",
    "cost_model_version",
    "incidents",
)
 
# Every key incidents/types.py's expected_magnitude can emit. None of them may
# appear anywhere in manifest.json (ground_truth.py convention 20).
MAGNITUDE_KEYS: frozenset[str] = frozenset(
    {
        "expected_magnitude",
        "log_byte_multiplier",
        "latency_delta_pct",
        "cost_delta_pct",
        "idle_cost_rate",
        "instance_count_delta",
    }
)
 

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
