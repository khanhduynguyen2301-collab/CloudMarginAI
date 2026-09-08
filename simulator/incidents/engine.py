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
(61-75), 2 in test (76-90, including exactly one logging_regression — the
M2 requirement). Non-overlap rule: no two incidents share overlapping
windows on the same service; no incident window crosses a split boundary.
"""
from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd

from simulator.incidents.types import IncidentType
from simulator.schema import DeploymentRow, GroundTruthIncidentRow, ResourceChangeRow
from simulator.topology import Organization, Service

# Causal-event tagging convention (docs/phase1/incident-injection-spec.md /
# the causal-event tagging convention it references) — the ONLY place
# these reserved values should be written. Background generators in
# changes/ must never emit these.
CAUSAL_EVENT_TAG: dict[IncidentType, tuple[str, str]] = {
    # incident_type -> (table, reserved value)
    IncidentType.LOGGING_REGRESSION: ("deployments", "logging-config"),
    IncidentType.QUERY_REGRESSION: ("deployments", "query-layer"),
    IncidentType.IDLE_ACCELERATOR: ("resource_changes", "schedule_removed"),
    IncidentType.AUTOSCALING_ERROR: ("resource_changes", "update_scaling_policy"),
}


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
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, DeploymentRow | ResourceChangeRow, GroundTruthIncidentRow]:
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
