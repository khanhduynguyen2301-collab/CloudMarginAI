"""Background deployment cadence generator — feeds `deployments`.

Implements docs/phase1/workload-cost-model.md, "Change events":
each service releases on an independent Poisson-ish cadence, mean
1 deployment per 6-9 days, `version` monotonically incrementing,
`changed_component` drawn from {api, worker, config, dependency}.

Do NOT use "logging-config" or "query-layer" as a changed_component value
here — those are reserved for incident-causing deployments written by
simulator/incidents/engine.py (see the causal-event tagging convention in
docs/phase1/incident-injection-spec.md). Background noise must stay
visually distinct from real causes, or the root-cause ranker's evaluation
in Phase 3 becomes meaningless.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from simulator.schema import DeploymentRow
from simulator.topology import Service


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
