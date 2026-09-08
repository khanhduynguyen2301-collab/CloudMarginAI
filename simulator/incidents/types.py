"""The four confirmed incident types, operationalized.

Transcribed exactly from docs/phase1/incident-injection-spec.md, "The four
incident types, operationalized" and "Ground-truth schema". Fully
implemented — this is a declarative table (magnitude ranges, durations,
which service each type targets), not a design decision left for
incidents/engine.py.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from simulator.topology import INCIDENT_SERVICE_MAP


class IncidentType(str, Enum):
    LOGGING_REGRESSION = "logging_regression"
    QUERY_REGRESSION = "query_regression"
    IDLE_ACCELERATOR = "idle_accelerator"
    AUTOSCALING_ERROR = "autoscaling_error"


@dataclass(frozen=True)
class IncidentSpec:
    incident_type: IncidentType
    service_name: str  # resolve against topology.build_topology(), project="prod"
    magnitude_low: float
    magnitude_high: float
    duration_hours_low: int
    duration_hours_high: int
    safe_remediation: str

    def expected_magnitude(self, sampled_value: float, **extra: float) -> dict:
        """Build the `expected_magnitude` JSON for a ground_truth_incidents row.

        `extra` carries a second value for incident types that need one
        (query_regression needs both latency_delta_pct and
        cost_delta_pct — see docs/phase0/incident-catalogue.md /
        docs/phase1/incident-injection-spec.md for exactly which key(s)
        each incident type uses).
        """
        raise NotImplementedError(
            "TODO: return the correct JSON shape for this incident_type, e.g. "
            '{"log_byte_multiplier": sampled_value} for LOGGING_REGRESSION'
        )


INCIDENT_SPECS: dict[IncidentType, IncidentSpec] = {
    IncidentType.LOGGING_REGRESSION: IncidentSpec(
        incident_type=IncidentType.LOGGING_REGRESSION,
        service_name=INCIDENT_SERVICE_MAP["logging_regression"],
        magnitude_low=3.5,
        magnitude_high=6.0,
        duration_hours_low=72,
        duration_hours_high=168,
        safe_remediation="Restore logging level",
    ),
    IncidentType.QUERY_REGRESSION: IncidentSpec(
        incident_type=IncidentType.QUERY_REGRESSION,
        service_name=INCIDENT_SERVICE_MAP["query_regression"],
        magnitude_low=1.8,  # queries/request multiplier; latency 1.5-2.5x is a second, correlated draw
        magnitude_high=3.0,
        duration_hours_low=48,
        duration_hours_high=120,
        safe_remediation="Rollback query change",
    ),
    IncidentType.IDLE_ACCELERATOR: IncidentSpec(
        incident_type=IncidentType.IDLE_ACCELERATOR,
        service_name=INCIDENT_SERVICE_MAP["idle_accelerator"],
        magnitude_low=0.0,  # gpu_utilization floor, held below 5%
        magnitude_high=0.05,
        duration_hours_low=96,
        duration_hours_high=240,
        safe_remediation="Stop or schedule the resource",
    ),
    IncidentType.AUTOSCALING_ERROR: IncidentSpec(
        incident_type=IncidentType.AUTOSCALING_ERROR,
        service_name=INCIDENT_SERVICE_MAP["autoscaling_error"],
        magnitude_low=3.0,  # min_replicas multiplier
        magnitude_high=5.0,
        duration_hours_low=48,
        duration_hours_high=96,
        safe_remediation="Restore scaling policy",
    ),
}
