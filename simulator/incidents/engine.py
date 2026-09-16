"""The injection engine: mutate one generator parameter for a bounded
window, never hand-edit output rows after the fact.

Implements docs/phase1/incident-injection-spec.md, "Injection engine
contract" and "Count and placement across the 90 days":

    inject(incident_type, service/resource, start_ts, duration_hours, magnitude) -> {
        mutate the relevant generator parameter for [start_ts, start_ts + duration_hours)
        optionally apply a remediation event at end_ts that reverts the parameter
        write one row to ground_truth_incidents
    }

10 total incidents, 2-3 per type: 6 in train (days 1-60), 2 in validation
(61-75), 2 in test (76-90, including exactly one logging_regression - the
M2 requirement). Non-overlap rule: no two incidents share overlapping
windows on the same service; no incident window crosses a split boundary.

Four conventions this module fixes, numbered on from resource_changes.py's:

14. INJECTIONS ARE PURE, AND EXPRESSED AS RATIOS. `inject` never modifies the
    frames it is handed; it returns new ones. And within the window it rewrites
    each clean value by the RATIO the parameter change implies - `usage x m`,
    `instance -> max(instance, raised_floor)`, `cpu x old/new` - rather than
    regenerating the window from scratch.

    Both halves matter. Purity keeps the clean series available, which is what
    lets the ground-truth magnitude be measured against it (convention 15).
    Ratio rewriting preserves each row's own noise draw, so the only thing that
    changes inside the window is the parameter. Regenerating would swap the
    noise realization too, putting a discontinuity at the window edge that a
    detector could find for entirely the wrong reason.

    The rewrite is exact, not an approximation, because every affected quantity
    is linear in its parameter: billing.py builds `logging.ingested-gb` as
    `requests x log_kb / 1e6` and `db.cpu-hour` as `requests x db_cpu_ms /
    3.6e6`, so scaling the parameter scales the usage; capacity.py derives
    `cpu` from `instance_count`, so raising the replica floor divides CPU by
    the same ratio.

    ONE EXCEPTION, in idle_accelerator: outside the job window the clean GPU
    count is 0, and a ratio against zero is undefined. Those hours get a fresh
    noise draw from the incidents stream. It is the only place in this module
    where a clean noise realization is replaced rather than scaled.

15. GROUND-TRUTH MAGNITUDE IS MEASURED, NOT COPIED. Only logging_regression
    records its input parameter (`log_byte_multiplier`). The other three record
    OUTCOMES - `latency_delta_pct`, `cost_delta_pct`, `idle_cost_rate`,
    `instance_count_delta` - computed here by differencing the injected frames
    against the clean ones. A consequence worth knowing: the sampled input
    magnitude is not recoverable from the ground-truth row, only by replaying
    `simulator_seed`.

16. COST IS RECOMPUTED FROM USAGE, never scaled alongside it. Every billing
    mutation goes through `_recost`, which rebuilds `credits` and
    `effective_cost` from `usage_amount x unit_price`. billing.py's convention 9
    (effective_cost reconstructs exactly) then holds inside incident windows by
    construction, rather than by three columns happening to be scaled in step.

17. NO INCIDENT TOUCHES DEMAND. None of the four types changes traffic - the
    spec is explicit for logging_regression ("traffic untouched"), and the other
    three mutate capacity or billing parameters only. `demand` is therefore
    returned unchanged and `application_activity_hourly` is a clean control
    series for the whole run. That is deliberate: it denies Phase 3 a
    business-demand shortcut, and it is why "cost rose while requests stayed
    flat" is the signature worth learning.
"""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd

from simulator.cost_model.pricing import CREDIT_RATE, SKU_PRICES
from simulator.incidents.types import INCIDENT_SPECS, IncidentType
from simulator.noise import unit_mean_lognormal
from simulator.schema import DeploymentRow, GroundTruthIncidentRow, ResourceChangeRow
from simulator.topology import ORGANIZATION_ID, Organization, Service
from simulator.workloads.capacity import _congestion, resource_id_for
from simulator.workloads.capacity import params_for as capacity_params_for

SCHEMA_VERSION = "1"

# Causal-event tagging convention (docs/phase1/incident-injection-spec.md /
# the causal-event tagging convention it references) - the ONLY place
# these reserved values should be written. Background generators in
# changes/ must never emit these.
CAUSAL_EVENT_TAG: dict[IncidentType, tuple[str, str]] = {
    # incident_type -> (table, reserved value)
    IncidentType.LOGGING_REGRESSION: ("deployments", "logging-config"),
    IncidentType.QUERY_REGRESSION: ("deployments", "query-layer"),
    IncidentType.IDLE_ACCELERATOR: ("resource_changes", "schedule_removed"),
    IncidentType.AUTOSCALING_ERROR: ("resource_changes", "update_scaling_policy"),
}

# The causal change lands slightly BEFORE the incident it causes, never at the
# same instant. A zero lag would let Phase 3 find the true cause with an
# equality join on the timestamp; a lead forces it to reason over a window, and
# it is what a real deploy-then-regress sequence looks like.
CAUSAL_LEAD_HOURS_LOW = 1
CAUSAL_LEAD_HOURS_HIGH = 3

# Provisional deployment_id numbering. Background deployments count from 0001
# per service, so this offset keeps the two generators from minting the same
# primary key. It is PROVISIONAL: cli.py must renumber all deployments
# chronologically when it merges the two streams, or the offset becomes a tell
# of its own. See docs/phase1/decisions.md.
CAUSAL_DEPLOYMENT_ID_OFFSET = 5000

CAUSAL_ACTORS: tuple[str, ...] = (
    "a.okafor@example.com",
    "j.lindqvist@example.com",
    "m.tran@example.com",
    "r.deshpande@example.com",
)

# Query regression raises the tail harder than the median: p99 takes the full
# sampled latency multiplier, p50 its square root. A flat multiplier on both
# would leave p99/p50 constant, which is not what a slow query looks like.
QUERY_LATENCY_MULT_LOW = 1.5
QUERY_LATENCY_MULT_HIGH = 2.5

# An idle GPU box still reports the memory of a loaded job. capacity.py parks
# off-hours memory at a fraction of the loaded baseline, so restoring "loaded"
# is a divide by that fraction - a ratio rewrite, convention 14.
_BATCH_IDLE_MEMORY_FRACTION = 0.25
_BATCH_CPU_PER_GPU = 0.45  # capacity.py._batch_frame
_IDLE_UTILIZATION_CEILING = 0.05  # spec: "GPU utilization held below 5%"
_BATCH_USAGE_SIGMA = 0.04  # billing.BillingParams.sigma_usage default

# --- placement ---------------------------------------------------------------

# Which incident types land in which split. Fixed rather than sampled, so 6/2/2
# and "exactly one logging_regression in test" are structural facts about the
# simulator instead of properties that hold for most seeds.
# Per-type totals: logging 3, query 3, idle 2, autoscaling 2 = 10.
SPLIT_ORDER: tuple[str, ...] = ("train", "validation", "test")
SPLIT_ALLOCATION: dict[str, tuple[IncidentType, ...]] = {
    "train": (
        IncidentType.LOGGING_REGRESSION,
        IncidentType.LOGGING_REGRESSION,
        IncidentType.QUERY_REGRESSION,
        IncidentType.QUERY_REGRESSION,
        IncidentType.IDLE_ACCELERATOR,
        IncidentType.AUTOSCALING_ERROR,
    ),
    "validation": (IncidentType.QUERY_REGRESSION, IncidentType.IDLE_ACCELERATOR),
    # M2's milestone - "flag a held-out logging regression with calibrated
    # bounds" - is satisfied by this split's logging_regression row.
    "test": (IncidentType.LOGGING_REGRESSION, IncidentType.AUTOSCALING_ERROR),
}

# Two incidents on the same service must not merely avoid overlapping - they
# have to be separated, or the pair reads as one long incident with a dip in the
# middle and Phase 2's window labelling cannot tell them apart.
MIN_GAP_HOURS_SAME_SERVICE = 48
PLACEMENT_MAX_ATTEMPTS = 2000


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def window_end(start_ts, duration_hours: int) -> pd.Timestamp:
    """Exclusive end of an incident window. Windows are half-open [start, end)."""
    return pd.Timestamp(start_ts) + pd.Timedelta(hours=int(duration_hours))


def _mask(frame: pd.DataFrame, start_ts, duration_hours: int) -> np.ndarray:
    hour = pd.DatetimeIndex(frame["hour"])
    start = pd.Timestamp(start_ts)
    return np.asarray((hour >= start) & (hour < window_end(start, duration_hours)))


def _sku_rows(frame: pd.DataFrame, start_ts, duration_hours: int, sku: str) -> np.ndarray:
    return _mask(frame, start_ts, duration_hours) & (frame["sku"] == sku).to_numpy()


def _recost(billing: pd.DataFrame, rows: np.ndarray) -> None:
    """Rebuild credits/effective_cost from usage_amount on `rows` (convention 16)."""
    prices = np.array(
        [SKU_PRICES[sku].unit_price_usd for sku in billing.loc[rows, "sku"]], dtype=float
    )
    list_cost = billing.loc[rows, "usage_amount"].to_numpy(dtype=float) * prices
    billing.loc[rows, "credits"] = list_cost * CREDIT_RATE
    billing.loc[rows, "effective_cost"] = list_cost * (1.0 - CREDIT_RATE)


def _cost_of(billing: pd.DataFrame, rows: np.ndarray) -> float:
    return float(billing.loc[rows, "effective_cost"].sum())


# ---------------------------------------------------------------------------
# the four mutations
# ---------------------------------------------------------------------------


def _inject_logging_regression(
    service: Service,
    metrics: pd.DataFrame,
    billing: pd.DataFrame,
    start_ts,
    duration_hours: int,
    magnitude: float,
    rng: np.random.Generator,
) -> dict:
    """Log verbosity multiplier on logging.ingested-gb usage.

    Traffic is untouched, so `metrics` is not modified at all: cost rises while
    every resource metric stays exactly on its clean path. That dissociation is
    the whole signature - and it is why this type is the M2 milestone, since
    nothing but the cost series can find it.
    """
    del service, metrics, rng
    rows = _sku_rows(billing, start_ts, duration_hours, "logging.ingested-gb")
    billing.loc[rows, "usage_amount"] *= magnitude
    _recost(billing, rows)
    return {}  # log_byte_multiplier is the input parameter, recorded directly


def _inject_query_regression(
    service: Service,
    metrics: pd.DataFrame,
    billing: pd.DataFrame,
    start_ts,
    duration_hours: int,
    magnitude: float,
    rng: np.random.Generator,
) -> dict:
    """Queries-per-request multiplier on db.cpu-hour, plus a correlated latency rise.

    The latency multiplier is a second, independent draw inside the doc's
    1.5-2.5x band: the spec gives the two magnitudes separate ranges, so they
    are not one number wearing two hats.
    """
    del service
    latency_mult = float(rng.uniform(QUERY_LATENCY_MULT_LOW, QUERY_LATENCY_MULT_HIGH))

    window = _mask(billing, start_ts, duration_hours)
    clean_cost = _cost_of(billing, window)
    db_rows = window & (billing["sku"] == "db.cpu-hour").to_numpy()
    billing.loc[db_rows, "usage_amount"] *= magnitude
    _recost(billing, db_rows)
    dirty_cost = _cost_of(billing, window)

    metric_rows = _mask(metrics, start_ts, duration_hours)
    metrics.loc[metric_rows, "latency_p99_ms"] *= latency_mult
    metrics.loc[metric_rows, "latency_p50_ms"] *= np.sqrt(latency_mult)

    return {
        "latency_delta_pct": (latency_mult - 1.0) * 100.0,
        "cost_delta_pct": (dirty_cost / clean_cost - 1.0) * 100.0,
    }


def _inject_idle_accelerator(
    service: Service,
    metrics: pd.DataFrame,
    billing: pd.DataFrame,
    start_ts,
    duration_hours: int,
    magnitude: float,
    rng: np.random.Generator,
) -> dict:
    """The job's schedule is forced "active": GPUs stay allocated around the
    clock with utilization pinned below 5%.

    Off-window hours are where the money goes - the clean frame has
    instance_count 0 there, so both the metric and the GPU-hour usage have to be
    created rather than scaled (convention 14's one exception). Hours inside the
    normal job window keep their billing untouched; only their utilization
    collapses, which is what tells a detector the job is running empty rather
    than merely running long.
    """
    params = capacity_params_for(service)
    rows = _mask(metrics, start_ts, duration_hours)
    n = int(rows.sum())
    if n == 0:
        return {"idle_cost_rate": 0.0}

    was_off = metrics.loc[rows, "instance_count"].to_numpy() == 0
    held = np.clip(
        magnitude * unit_mean_lognormal(rng, params.sigma_cpu, n),
        0.0,
        _IDLE_UTILIZATION_CEILING,
    )
    metrics.loc[rows, "gpu_utilization"] = held
    metrics.loc[rows, "cpu_utilization"] = held * _BATCH_CPU_PER_GPU
    metrics.loc[rows, "instance_count"] = params.gpu_count
    memory = metrics.loc[rows, "memory_utilization"].to_numpy(dtype=float)
    metrics.loc[rows, "memory_utilization"] = np.clip(
        np.where(was_off, memory / _BATCH_IDLE_MEMORY_FRACTION, memory), 0.0, 1.0
    )

    gpu_rows = _sku_rows(billing, start_ts, duration_hours, "compute.gpu-hour")
    clean_gpu_cost = _cost_of(billing, gpu_rows)
    usage = billing.loc[gpu_rows, "usage_amount"].to_numpy(dtype=float, copy=True)
    idle_hours = usage == 0.0
    usage[idle_hours] = params.gpu_count * unit_mean_lognormal(
        rng, _BATCH_USAGE_SIGMA, int(idle_hours.sum())
    )
    billing.loc[gpu_rows, "usage_amount"] = usage
    _recost(billing, gpu_rows)

    wasted = _cost_of(billing, gpu_rows) - clean_gpu_cost
    return {"idle_cost_rate": wasted / float(duration_hours)}


def _inject_autoscaling_error(
    service: Service,
    metrics: pd.DataFrame,
    billing: pd.DataFrame,
    start_ts,
    duration_hours: int,
    magnitude: float,
    rng: np.random.Generator,
) -> dict:
    """min_replicas raised 3-5x, decoupling instance_count from requests.

    capacity.py computes instance_count as clip(ceil(needed), min_replicas,
    max_replicas). Raising only the floor is therefore exactly
    min(max(clean, raised_floor), max_replicas) - no need to recover `needed`
    from the rounded request_count, and no rounding drift.

    Everything downstream follows by ratio: CPU is derived from instance_count
    so it falls by old/new, latency follows CPU through the same congestion
    curve, memory follows CPU through its elasticity, and vCPU-hour usage rises
    by new/old. The result is the correlated signature the spec asks for - cost
    up, utilization down, latency slightly BETTER - which is exactly what
    separates this from a genuine traffic surge.
    """
    del rng
    params = capacity_params_for(service)
    raised_floor = min(int(round(params.min_replicas * magnitude)), params.max_replicas)

    rows = _mask(metrics, start_ts, duration_hours)
    clean_instances = metrics.loc[rows, "instance_count"].to_numpy(dtype=float)
    new_instances = np.minimum(
        np.maximum(clean_instances, float(raised_floor)), float(params.max_replicas)
    )

    clean_cpu = metrics.loc[rows, "cpu_utilization"].to_numpy(dtype=float)
    new_cpu = np.clip(clean_cpu * (clean_instances / new_instances), 1e-4, 1.0)

    metrics.loc[rows, "instance_count"] = new_instances.astype(np.int64)
    metrics.loc[rows, "cpu_utilization"] = new_cpu
    metrics.loc[rows, "memory_utilization"] = np.clip(
        metrics.loc[rows, "memory_utilization"].to_numpy(dtype=float)
        + params.memory_elasticity * (new_cpu - clean_cpu),
        1e-4,
        1.0,
    )
    congestion_ratio = _congestion(new_cpu, params.target_utilization) / _congestion(
        clean_cpu, params.target_utilization
    )
    metrics.loc[rows, "latency_p50_ms"] *= congestion_ratio
    metrics.loc[rows, "latency_p99_ms"] *= congestion_ratio

    # Scale vCPU-hour usage by the same instance ratio, matched on (region, hour).
    scale = pd.Series(
        new_instances / clean_instances,
        index=pd.MultiIndex.from_arrays(
            [
                metrics.loc[rows, "region"].to_numpy(),
                pd.DatetimeIndex(metrics.loc[rows, "hour"]),
            ]
        ),
    )
    cpu_rows = _sku_rows(billing, start_ts, duration_hours, "compute.vcpu-hour")
    key = pd.MultiIndex.from_arrays(
        [
            billing.loc[cpu_rows, "region"].to_numpy(),
            pd.DatetimeIndex(billing.loc[cpu_rows, "hour"]),
        ]
    )
    billing.loc[cpu_rows, "usage_amount"] *= scale.reindex(key).to_numpy()
    _recost(billing, cpu_rows)

    # Service-wide extra instances per hour, averaged over the window.
    extra_per_hour = (
        pd.Series(new_instances - clean_instances)
        .groupby(pd.DatetimeIndex(metrics.loc[rows, "hour"]).to_numpy())
        .sum()
    )
    return {"instance_count_delta": float(extra_per_hour.mean())}


_MUTATIONS = {
    IncidentType.LOGGING_REGRESSION: _inject_logging_regression,
    IncidentType.QUERY_REGRESSION: _inject_query_regression,
    IncidentType.IDLE_ACCELERATOR: _inject_idle_accelerator,
    IncidentType.AUTOSCALING_ERROR: _inject_autoscaling_error,
}


# ---------------------------------------------------------------------------
# causal event
# ---------------------------------------------------------------------------


def _causal_event(
    incident_type: IncidentType,
    service: Service,
    start_ts: pd.Timestamp,
    ordinal: int,
    rng: np.random.Generator,
) -> DeploymentRow | ResourceChangeRow:
    """The change that caused this incident, tagged with its reserved value.

    Lands CAUSAL_LEAD_HOURS_LOW..HIGH hours before the incident starts;
    place_incidents guarantees there is room for that lead inside the split.
    """
    table, reserved = CAUSAL_EVENT_TAG[incident_type]
    lead = int(rng.integers(CAUSAL_LEAD_HOURS_LOW, CAUSAL_LEAD_HOURS_HIGH + 1))
    at = pd.Timestamp(start_ts) - pd.Timedelta(hours=lead)
    actor = str(rng.choice(CAUSAL_ACTORS))

    if table == "deployments":
        return DeploymentRow(
            organization_id=ORGANIZATION_ID,
            service=service.name,
            deployment_id=f"dep-{service.name}-{CAUSAL_DEPLOYMENT_ID_OFFSET + ordinal:04d}",
            released_at=at,
            version=f"v2.{ordinal}.0",
            changed_component=reserved,
            actor=actor,
            schema_version=SCHEMA_VERSION,
            ingested_at=at,
        )

    # resource_changes is resource-grained, but both incidents it carries are
    # policy changes that apply to the whole service. The row is attached to the
    # primary-region resource, so Phase 3 must score an autoscaling cause at
    # SERVICE level rather than by exact resource match. See decisions.md.
    params = capacity_params_for(service)
    primary = max(params.region_weights, key=lambda r: (params.region_weights[r], r))
    if reserved == "schedule_removed":
        before = {"schedule": "0 2 * * *", "auto_stop": True}
        after = {"schedule": None, "auto_stop": False}
    else:
        before = {"min_replicas": params.min_replicas}
        after = {"min_replicas": min(params.min_replicas * 4, params.max_replicas)}

    return ResourceChangeRow(
        organization_id=ORGANIZATION_ID,
        project_id=service.project,
        resource_id=resource_id_for(service, primary),
        changed_at=at,
        change_type=reserved,
        actor=actor,
        before_config=before,
        after_config=after,
        schema_version=SCHEMA_VERSION,
        ingested_at=at,
    )


def inject(
    incident_type: IncidentType,
    service: Service,
    start_ts: datetime,
    duration_hours: int,
    magnitude: float,
    demand: pd.DataFrame,
    metrics: pd.DataFrame,
    billing: pd.DataFrame,
    rng: np.random.Generator,
    *,
    organization_id: str,
    chronological_split: str,
    simulator_seed: int,
) -> tuple[
    pd.DataFrame,
    pd.DataFrame,
    pd.DataFrame,
    DeploymentRow | ResourceChangeRow,
    GroundTruthIncidentRow,
]:
    """Mutate `demand`/`metrics`/`billing` in place for [start_ts, start_ts + duration_hours)
    per this incident_type's spec (simulator.incidents.types.INCIDENT_SPECS),
    write the causal event using CAUSAL_EVENT_TAG, and build the
    ground_truth_incidents row.

    Args:
        rng: this component's Generator — get it via
            `simulator.seeding.rng_for(master_seed, "incidents")`.

    Returns:
        (demand, metrics, billing, causal_event, ground_truth_row) — the
        first three are the same DataFrames passed in, mutated; decide and
        document whether you mutate in place or return copies, and use
        that convention consistently across all four incident types.
    """
    raise NotImplementedError(
        "TODO: implement the four incident-type-specific mutations "
        "(see docs/phase1/incident-injection-spec.md, 'The four incident "
        "types, operationalized' for the exact parameter each type mutates)"
    )


def place_incidents(
    topology: Organization,
    hours: pd.DatetimeIndex,
    split_boundaries: dict[str, tuple[datetime, datetime]],
    rng: np.random.Generator,
) -> list[tuple[IncidentType, Service, datetime, int, float]]:
    """Decide how many incidents, of which type, on which service, starting when.

    Implements the "Count and placement" table: 10 incidents total
    (2-3 per type), 6/2/2 across train/validation/test, exactly one
    logging_regression in test. Enforce non-overlap: no two incidents
    share a time window on the same service, and no window crosses a
    split boundary.

    Args:
        rng: this component's Generator — get it via
            `simulator.seeding.rng_for(master_seed, "incidents")` (same
            component as `inject` — placement and magnitude sampling are
            both part of the "incidents" random stream).

    Returns:
        List of (incident_type, service, start_ts, duration_hours,
        magnitude) tuples, one per incident, ready to pass to `inject`.
    """
    raise NotImplementedError("TODO: implement the placement/scheduling algorithm above")
