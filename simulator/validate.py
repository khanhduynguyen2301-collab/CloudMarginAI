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
# Causal-check tolerances (measured; see module docstring)
# ---------------------------------------------------------------------------
 
BASELINE_WEEK_SHIFTS: tuple[int, ...] = (-1, 1, -2, 2, -3, 3, -4, 4)
 
# requests ratio, window vs baseline. Observed 0.977-1.054.
TRAFFIC_FLAT_TOL = 0.10
# implied / declared log multiplier. Observed within 0.8%.
LOG_MULTIPLIER_RTOL = 0.05
# measured / declared, for p99 latency and total service cost. Observed within
# 1.6% and 4.3%.
LATENCY_RTOL = 0.10
COST_RTOL = 0.10
# measured / declared idle cost per hour. Observed within 0.5%.
IDLE_COST_RTOL = 0.05
# per-hour GPU-hour usage vs the pool size. Observed up to 14.8% (one hour in
# ~5,000, at sigma_usage 0.04 - a ~3.7 sigma draw).
GPU_USAGE_RTOL = 0.25
# Measured excess over 1.0 must reach this fraction of the spec's minimum excess.
# The weakest db.cpu ratio observed was 1.829 against a 1.8 floor.
RISE_FRACTION = 0.8
# "GPU utilization held below 5%". engine.py clips AT 0.05, and exactly 0.05
# occurs, so the bound is inclusive.
IDLE_GPU_UTILIZATION_MAX = 0.05
# reconstruction of effective_cost from usage x price
RECONSTRUCTION_RTOL = 1e-9
 
_MESSAGE_CAP = 10  # row-level failures are summarised past this many examples
 
_UNION_ORIGINS = (Union, types.UnionType)
 

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
 
 
def _unwrap(hint: Any) -> tuple[Any, bool]:
    """(base type, nullable-by-annotation) for a schema.py field hint."""
    if get_origin(hint) in _UNION_ORIGINS:
        args = [a for a in get_args(hint) if a is not type(None)]
        return args[0], True
    return hint, False
 
 
def _dtype_problem(series: pd.Series, base: Any) -> str | None:
    """Why `series` cannot hold values of `base`, or None if it can."""
    kind = series.dtype.kind
    if base is float:
        return None if kind == "f" else f"expected float, got {series.dtype}"
    if base is int:
        return None if kind in "iu" else f"expected integer, got {series.dtype}"
    if base is bool:
        return None if kind == "b" else f"expected bool, got {series.dtype}"
    if base is datetime:
        if not isinstance(series.dtype, pd.DatetimeTZDtype):
            return f"expected tz-aware UTC timestamps, got {series.dtype}"
        if str(series.dtype.tz) != "UTC":
            return f"expected UTC, got tz={series.dtype.tz}"
        return None
    if base is str:
        inferred = pd.api.types.infer_dtype(series, skipna=True)
        return None if inferred in ("string", "empty") else f"expected strings, got {inferred}"
    if base is dict:
        if not series.map(lambda v: isinstance(v, dict)).all():
            return "expected a dict (JSON object) in every row"
        return None
    return f"no dtype rule for annotation {base!r}"
 
 
def _examples(values, cap: int = _MESSAGE_CAP) -> str:
    values = list(values)
    shown = ", ".join(str(v) for v in values[:cap])
    return shown + (f", ... ({len(values) - cap} more)" if len(values) > cap else "")
 
 
def _resource_kinds() -> dict[tuple[str, str], ResourceKind]:
    return {(s.project, s.name): s.resource_kind for s in build_topology().all_services()}
 
 
# ---------------------------------------------------------------------------
# Data-quality gates (validation-plan.md, "Data-quality gates")
# ---------------------------------------------------------------------------
 
 
def check_schema(tables: dict[str, pd.DataFrame]) -> list[str]:
    """Every output row matches its table's field set/types in
    docs/phase0/schema-v1.md / docs/phase0/incident-catalogue.md.
 
    Field sets and types come from schema.py, the typed mirror of the frozen
    docs. Also refuses any table not in TABLE_ROWS - most importantly ground
    truth, which must never travel alongside the analytical tables.
    """
    failures: list[str] = []
    for name in sorted(set(tables) - set(TABLE_ROWS)):
        if "ground" in name or "truth" in name:
            failures.append(
                f"{name!r} is bundled with the analytical tables - ground truth must be "
                "written separately (labels/ground_truth.py convention 18), never passed here"
            )
        else:
            failures.append(f"unexpected table {name!r}; expected only {sorted(TABLE_ROWS)}")
 
    for name, row_type in TABLE_ROWS.items():
        if name not in tables:
            failures.append(f"missing table {name!r}")
            continue
        frame = tables[name]
        expected = [f for f in get_type_hints(row_type)]
        missing = [c for c in expected if c not in frame.columns]
        extra = [c for c in frame.columns if c not in expected]
        if missing:
            failures.append(f"{name}: missing columns {missing}")
        if extra:
            failures.append(f"{name}: columns not in the schema {extra}")
        if frame.empty:
            failures.append(f"{name}: table is empty")
            continue
        for column, hint in get_type_hints(row_type).items():
            if column not in frame.columns:
                continue
            base, _ = _unwrap(hint)
            problem = _dtype_problem(frame[column], base)
            if problem:
                failures.append(f"{name}.{column}: {problem}")
 
    billing = tables.get("billing_hourly")
    if billing is not None and "source" in billing.columns:
        bad = set(billing["source"].dropna().unique()) - {"estimated", "billing_export"}
        if bad:
            failures.append(f"billing_hourly.source has values outside the schema enum: {bad}")
    return failures
 
 
def check_uniqueness(tables: dict[str, pd.DataFrame]) -> list[str]:
    """No duplicate rows on each table's declared grain (e.g.
    project-service-SKU-region-hour for billing_hourly)."""
    failures: list[str] = []
    for name, grain in TABLE_GRAIN.items():
        frame = tables.get(name)
        if frame is None or not set(grain) <= set(frame.columns):
            continue  # check_schema reports it
        duplicated = frame[frame.duplicated(list(grain), keep=False)]
        if not duplicated.empty:
            keys = duplicated[list(grain)].drop_duplicates().head(_MESSAGE_CAP)
            examples = [tuple(row) for row in keys.itertuples(index=False)]
            failures.append(
                f"{name}: {len(duplicated)} rows share a grain key {grain}; e.g. {examples}"
            )
    return failures
 
 
def check_nulls(tables: dict[str, pd.DataFrame]) -> list[str]:
    """No unexpected nulls outside the documented nullable fields.
 
    `revenue` may always be null. gpu_utilization must be null for exactly the
    services with no GPU, and latency must be null for exactly the batch jobs -
    see ALWAYS_NULLABLE for why the converse matters.
    """
    failures: list[str] = []
    for name, frame in tables.items():
        if name not in TABLE_ROWS:
            continue
        allowed = ALWAYS_NULLABLE.get(name, frozenset())
        conditional = (
            {_GPU_COLUMN, *_LATENCY_COLUMNS} if name == "resource_metrics_hourly" else set()
        )
        for column in frame.columns:
            if column in allowed or column in conditional:
                continue
            n = int(frame[column].isna().sum())
            if n:
                failures.append(f"{name}.{column}: {n} unexpected null(s)")
 
    metrics = tables.get("resource_metrics_hourly")
    if metrics is None or not {"project_id", "service", _GPU_COLUMN}.issubset(metrics.columns):
        return failures
    kinds = _resource_kinds()
    for (project, service), group in metrics.groupby(["project_id", "service"], sort=True):
        kind = kinds.get((project, service))
        if kind is None:
            continue  # check_referential_integrity reports it
        is_batch = kind is ResourceKind.BATCH_ACCELERATOR
        gpu_null = group[_GPU_COLUMN].isna()
        if is_batch and gpu_null.any():
            failures.append(
                f"{project}/{service}: {int(gpu_null.sum())} null gpu_utilization on a GPU service"
            )
        if not is_batch and not gpu_null.all():
            failures.append(
                f"{project}/{service}: gpu_utilization is set on a service with no GPU - a value "
                "here reads as an idle accelerator"
            )
        for column in _LATENCY_COLUMNS:
            if column in group.columns and not is_batch and group[column].isna().any():
                failures.append(
                    f"{project}/{service}: {int(group[column].isna().sum())} null {column} on a "
                    "request-serving service"
                )
    return failures
 
 
def check_cost_signs(billing: pd.DataFrame) -> list[str]:
    """usage_amount, effective_cost, credits are all >= 0 - and finite, since an
    infinite cost passes `>= 0` and would poison every sum downstream."""
    failures: list[str] = []
    for column in ("usage_amount", "effective_cost", "credits"):
        if column not in billing.columns:
            continue
        values = billing[column].to_numpy(dtype=float)
        negative = int((values < 0).sum())
        non_finite = int((~np.isfinite(values)).sum())
        if negative:
            failures.append(f"billing_hourly.{column}: {negative} negative value(s)")
        if non_finite:
            failures.append(f"billing_hourly.{column}: {non_finite} non-finite value(s)")
    return failures
 
 
def check_unit_consistency(billing: pd.DataFrame) -> list[str]:
    """Every SKU's usage_unit matches simulator.cost_model.pricing.SKU_PRICES;
    no mixed units within a SKU."""
    failures: list[str] = []
    if not {"sku", "usage_unit"}.issubset(billing.columns):
        return failures
    for sku, units in billing.groupby("sku")["usage_unit"].unique().items():
        if sku not in SKU_PRICES:
            failures.append(f"billing_hourly: SKU {sku!r} is not in the price table")
            continue
        if len(units) > 1:
            failures.append(f"billing_hourly: SKU {sku!r} mixes units {sorted(units)}")
        elif units[0] != SKU_PRICES[sku].usage_unit:
            failures.append(
                f"billing_hourly: SKU {sku!r} billed in {units[0]!r}, "
                f"price table says {SKU_PRICES[sku].usage_unit!r}"
            )
    return failures
 
 
def _find_magnitude_keys(obj: Any, path: str = "manifest") -> list[str]:
    hits: list[str] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in MAGNITUDE_KEYS or "magnitude" in str(key):
                hits.append(f"{path}.{key}")
            hits.extend(_find_magnitude_keys(value, f"{path}.{key}"))
    elif isinstance(obj, (list, tuple)):
        for i, value in enumerate(obj):
            hits.extend(_find_magnitude_keys(value, f"{path}[{i}]"))
    return hits
 
 
def check_lineage(tables: dict[str, pd.DataFrame], manifest: dict) -> list[str]:
    """Every row carries organization_id and schema_version; the run carries a
    manifest.json (seed, split boundaries, incident list).
 
    Also: ingested_at equals each row's own event time (convention 11 - the
    clock never enters the pipeline), and the manifest carries no ground-truth
    magnitude anywhere (convention 20).
    """
    failures: list[str] = []
    for name, frame in tables.items():
        if name not in TABLE_ROWS:
            continue
        if "organization_id" in frame.columns:
            wrong = set(frame["organization_id"].dropna().unique()) - {ORGANIZATION_ID}
            if wrong:
                failures.append(
                    f"{name}: organization_id values other than {ORGANIZATION_ID!r}: {wrong}"
                )
        if "schema_version" in frame.columns:
            versions = frame["schema_version"].dropna().unique()
            if len(versions) != 1 or not str(versions[0]).strip():
                failures.append(
                    f"{name}: expected one non-empty schema_version, got {list(versions)}"
                )
        event = EVENT_TIME_COLUMN.get(name)
        if event and {"ingested_at", event}.issubset(frame.columns):
            drift = int((frame["ingested_at"] != frame[event]).sum())
            if drift:
                failures.append(
                    f"{name}: {drift} row(s) where ingested_at != {event} - wall-clock time has "
                    "entered the pipeline, which breaks the byte-identical rerun"
                )
 
    if not isinstance(manifest, dict):
        return failures + ["manifest is missing or not a JSON object"]
    missing = [k for k in REQUIRED_MANIFEST_KEYS if k not in manifest]
    if missing:
        failures.append(f"manifest.json is missing {missing}")
    leaks = _find_magnitude_keys(manifest)
    if leaks:
        failures.append(
            f"manifest.json carries ground-truth magnitude at {leaks} - it must exist only in "
            "ground_truth_incidents (labels/ground_truth.py convention 20)"
        )
    return failures
 
 
def check_referential_integrity(tables: dict[str, pd.DataFrame]) -> list[str]:
    """Every service, project and resource referenced anywhere exists.
 
    Not in validation-plan.md's list, and added deliberately: Phase 3 cites a
    config change by joining resource_changes.resource_id to the resource whose
    metrics moved. An orphaned id does not raise anywhere - the join just comes
    back empty and the ranker looks worse for no reason.
    """
    failures: list[str] = []
    topology = set(_resource_kinds())
    prod_services = {name for project, name in topology if project == "prod"}
 
    for name in ("billing_hourly", "resource_metrics_hourly"):
        frame = tables.get(name)
        if frame is None or not {"project_id", "service"}.issubset(frame.columns):
            continue
        seen = set(frame[["project_id", "service"]].drop_duplicates().itertuples(index=False))
        unknown = {tuple(x) for x in seen} - topology
        if unknown:
            failures.append(f"{name}: (project, service) pairs not in the topology: {unknown}")
 
    deployments = tables.get("deployments")
    if deployments is not None and "service" in deployments.columns:
        unknown = set(deployments["service"].unique()) - prod_services
        if unknown:
            failures.append(f"deployments: services not in prod: {unknown}")
 
    metrics = tables.get("resource_metrics_hourly")
    changes = tables.get("resource_changes")
    if metrics is not None and changes is not None and "resource_id" in changes.columns:
        orphans = set(changes["resource_id"].unique()) - set(metrics["resource_id"].unique())
        if orphans:
            failures.append(
                f"resource_changes: resource_ids with no metrics: {_examples(sorted(orphans))}"
            )
    return failures
 
 
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
