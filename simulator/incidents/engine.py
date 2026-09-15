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
