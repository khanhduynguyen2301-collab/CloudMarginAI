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

        Only LOGGING_REGRESSION records its input parameter. The other three
        record OUTCOMES, which incidents/engine.py measures by differencing the
        injected frames against the clean ones and passes in via `extra`
        (engine.py convention 15). `sampled_value` is therefore used by exactly
        one branch here and ignored by the rest.

        Raises:
            KeyError: `extra` is missing a measurement this incident type
                needs. A loud failure beats a ground-truth row with a silently
                absent magnitude, which evaluation code would read as real.
        """
        if self.incident_type is IncidentType.LOGGING_REGRESSION:
            return {"log_byte_multiplier": float(sampled_value)}
        if self.incident_type is IncidentType.QUERY_REGRESSION:
            return {
                "latency_delta_pct": float(extra["latency_delta_pct"]),
                "cost_delta_pct": float(extra["cost_delta_pct"]),
            }
        if self.incident_type is IncidentType.IDLE_ACCELERATOR:
            return {"idle_cost_rate": float(extra["idle_cost_rate"])}
        if self.incident_type is IncidentType.AUTOSCALING_ERROR:
            return {"instance_count_delta": float(extra["instance_count_delta"])}
        raise ValueError(f"no expected_magnitude shape for {self.incident_type!r}")


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
        # queries/request multiplier; latency 1.5-2.5x is a second, correlated draw
        magnitude_low=1.8,
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
