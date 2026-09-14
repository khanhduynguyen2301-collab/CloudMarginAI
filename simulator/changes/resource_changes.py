"""Background configuration-change generator — feeds `resource_changes`.

Implements docs/phase1/workload-cost-model.md, "Change events": a background
rate of ~1 benign config change per service per ~20 days (label/tag edits,
non-incident-causing), so that "a change happened near this time" is not, by
itself, evidence of an incident.

Convention 12 (from deployments.py) applies here too: "schedule_removed" and
"update_scaling_policy" are written only by simulator/incidents/engine.py, which
uses them to mark the configuration change that caused an incident. Background
noise must never emit them.

Unlike deployments, `resource_changes` carries `project_id`, so this generator
covers staging as well as prod — which is what gives staging any change activity
at all (deployments.py convention 13).

Every row points at a real resource_id from capacity.py, so Phase 3 can join a
candidate change to the resource whose metrics moved. Config changes are not
business-hours-clustered the way releases are: half of them are automated
housekeeping, and the rest are as likely to happen while someone is poking at a
console at 22:00 as at midday.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from simulator.schema import ResourceChangeRow
from simulator.topology import ORGANIZATION_ID, Service
from simulator.workloads.capacity import params_for as capacity_params_for
from simulator.workloads.capacity import resource_id_for

SCHEMA_VERSION = "1"


MEAN_DAYS_BETWEEN_CHANGES = 20.0

# Benign change types with the config shape each one edits. "schedule_removed"
# and "update_scaling_policy" are reserved — convention 12.
BENIGN_CHANGE_TYPES: tuple[str, ...] = (
    "update_label",
    "update_tag",
    "update_description",
    "update_budget_alert",
    "rotate_service_account_key",
)

CHANGE_ACTORS: tuple[str, ...] = (
    "a.okafor@example.com",
    "p.nwosu@example.com",
    "terraform-ci@example.iam.gserviceaccount.com",
    "s.whitfield@example.com",
    "config-sync@example.iam.gserviceaccount.com",
)

_COST_CENTRES: tuple[str, ...] = ("cc-1042", "cc-1108", "cc-2270")
_TIERS: tuple[str, ...] = ("gold", "silver", "bronze")
_BUDGET_ALERTS: tuple[int, ...] = (5, 15, 25)


def _config_pair(change_type: str, rng: np.random.Generator) -> tuple[dict, dict]:
    """before/after JSON for a benign change. Both sides are always populated so
    a consumer never has to guess what a null means."""
    if change_type == "update_label":
        before, after = rng.choice(_COST_CENTRES, size=2, replace=False)
        return {"labels": {"cost-centre": str(before)}}, {"labels": {"cost-centre": str(after)}}
    if change_type == "update_tag":
        before, after = rng.choice(_TIERS, size=2, replace=False)
        return {"tags": {"tier": str(before)}}, {"tags": {"tier": str(after)}}
    if change_type == "update_description":
        return {"description": "owned by platform"}, {
            "description": "owned by platform (see runbook)"
        }
    if change_type == "update_budget_alert":
        before, after = rng.choice(_BUDGET_ALERTS, size=2, replace=False)
        return {"budget_alert_usd": int(before)}, {"budget_alert_usd": int(after)}
    if change_type == "rotate_service_account_key":
        return {"key_version": 1}, {"key_version": 2}
    raise ValueError(f"no config shape defined for change_type {change_type!r}")


def generate_resource_changes(
    service: Service,
    hours: pd.DatetimeIndex,
    rng: np.random.Generator,
) -> list[ResourceChangeRow]:
    """Generate the background configuration-change history for one service.

    Args:
        service: the Service whose resources are changing.
        hours: the full run's hourly timestamp index (UTC).
        rng: this component's Generator — get it via
            `simulator.seeding.rng_for(master_seed, "changes")` (same
            component as deployments.py — both are "background change
            noise").

    Returns:
        List of ResourceChangeRow with benign change_type values (e.g.
        "update_label", "update_tag") and before_config/after_config
        JSON-serializable dicts.
    """
    raise NotImplementedError("TODO: implement the background config-change rate above")
