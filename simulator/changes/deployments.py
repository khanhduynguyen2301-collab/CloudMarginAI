"""Background deployment cadence generator — feeds `deployments`.

Implements docs/phase1/workload-cost-model.md, "Change events": each service
releases on an independent Poisson-ish cadence, mean 1 deployment per 6-9 days,
`version` monotonically incrementing, `changed_component` drawn from
{api, worker, config, dependency}.

Two conventions this module fixes, numbered on from billing.py's:

12. RESERVED VOCABULARY IS OFF LIMITS. "logging-config" and "query-layer" are
    written only by simulator/incidents/engine.py, which uses them to mark the
    deployment that caused an incident (see CAUSAL_EVENT_TAG there). Background
    noise must never emit them, or Phase 3's root-cause ranker could find a true
    cause by string match instead of by evidence — which would make its Top-3
    accuracy meaningless.
13. Deployments are generated for PROD services only. The frozen `deployments`
    schema carries `organization_id` and `service` but no `project_id`, so a row
    cannot say which project it hit — and prod and staging both contain
    web-frontend and checkout-api. Emitting for both would produce rows that are
    genuinely ambiguous, giving the ranker staging deploys as indistinguishable
    candidates for prod incidents. Staging change activity is represented in
    `resource_changes`, which does carry project_id.

Releases cluster in weekday business hours. That is realistic, and it makes
temporal proximity a signal the ranker has to work for: an incident beginning at
03:00 is much less likely to be deploy-caused than one beginning at 14:00.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from simulator.schema import DeploymentRow
from simulator.topology import ORGANIZATION_ID, Service

SCHEMA_VERSION = "1"

# Mean days between releases, per service. Within the doc's 6-9 day band;
# user-facing services ship a little more often than workers.
MEAN_DAYS_BETWEEN_RELEASES: dict[str, float] = {
    "web-frontend": 6.0,
    "checkout-api": 7.0,
    "recommendation-api": 6.5,
    "ingestion-worker": 8.0,
    "billing-worker": 9.0,
    "ml-training-job": 8.5,
}
DEFAULT_MEAN_DAYS_BETWEEN_RELEASES = 7.5

# Benign components only. "logging-config" and "query-layer" are reserved —
# convention 12.
CHANGED_COMPONENTS: tuple[str, ...] = ("api", "worker", "config", "dependency")

# Relative likelihood of a release landing in a given hour. Deploys happen
# during the working day; the occasional evening or weekend release exists but
# is rare.
BUSINESS_HOURS_UTC = range(9, 18)
WEIGHT_BUSINESS_HOURS = 1.0
WEIGHT_OFF_HOURS = 0.12
WEIGHT_WEEKEND = 0.04

# A small deterministic roster per service, so `actor` is stable across runs.
RELEASE_ACTORS: tuple[str, ...] = (
    "a.okafor@example.com",
    "j.lindqvist@example.com",
    "m.tran@example.com",
    "r.deshpande@example.com",
    "s.whitfield@example.com",
)

# Most releases bump the patch; a minority bump the minor and reset patch.
MINOR_BUMP_PROBABILITY = 0.18


def generate_deployments(
    service: Service,
    hours: pd.DatetimeIndex,
    rng: np.random.Generator,
) -> list[DeploymentRow]:
    """Generate the background deployment history for one service over `hours`.

    Args:
        service: the Service releasing deployments.
        hours: the full run's hourly timestamp index (UTC).
        rng: this component's Generator — get it via
            `simulator.seeding.rng_for(master_seed, "changes")`.

    Returns:
        List of DeploymentRow (organization_id/schema_version/ingested_at
        can be filled with placeholder/constant values here and
        overwritten by the caller if needed).
    """
    raise NotImplementedError("TODO: implement the Poisson-ish deployment cadence above")
