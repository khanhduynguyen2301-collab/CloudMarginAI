"""Background configuration-change generator — feeds `resource_changes`.

Implements docs/phase1/workload-cost-model.md, "Change events": background
rate of ~1 benign config change per service per ~20 days (label/tag edits,
non-incident-causing) — "a change happened near this time" must not, by
itself, be evidence of an incident.

Do NOT use "schedule_removed" or "update_scaling_policy" as a change_type
value here — those are reserved for incident-causing resource_changes
written by simulator/incidents/engine.py (see the causal-event tagging
convention in docs/phase1/incident-injection-spec.md).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from simulator.schema import ResourceChangeRow
from simulator.topology import Service


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
