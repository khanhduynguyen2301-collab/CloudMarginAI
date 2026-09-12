"""Row-level dataclasses matching the frozen table schemas.

Field names and types are transcribed exactly from:
  - docs/phase0/schema-v1.md            (BillingHourlyRow ... ResourceChangeRow)
  - docs/phase0/incident-catalogue.md + docs/phase1/incident-injection-spec.md
    (GroundTruthIncidentRow, extended with duration_hours/chronological_split)

Do not rename or retype a field here without updating the corresponding
frozen doc first — schema.py should never be the source of truth, only a
typed mirror of it. If a doc and this file disagree, the doc wins and this
file has a bug.

These dataclasses are fully implemented (they're structure, not logic) —
generators in workloads/, cost_model/, changes/, incidents/, and labels/
are what actually populate instances of them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class BillingHourlyRow:
    """docs/phase0/schema-v1.md, `billing_hourly` — grain: project-service-SKU-region-hour."""

    organization_id: str
    project_id: str
    service: str
    sku: str
    region: str
    hour: datetime
    usage_amount: float
    usage_unit: str
    credits: float
    effective_cost: float
    currency: str
    is_reconciled: bool
    source: str  # "estimated" | "billing_export" — always "estimated" from this simulator
    schema_version: str
    ingested_at: datetime


@dataclass
class ResourceMetricsHourlyRow:
    """docs/phase0/schema-v1.md, `resource_metrics_hourly` — grain: resource-hour."""

    organization_id: str
    project_id: str
    service: str
    resource_id: str
    region: str
    hour: datetime
    cpu_utilization: Optional[float]
    memory_utilization: Optional[float]
    gpu_utilization: Optional[float]  # None for non-GPU services
    instance_count: int
    request_count: int
    error_count: int
    latency_p50_ms: Optional[float]
    latency_p99_ms: Optional[float]
    schema_version: str
    ingested_at: datetime


@dataclass
class ApplicationActivityHourlyRow:
    """docs/phase0/schema-v1.md, `application_activity_hourly` — grain: product-service-hour."""

    organization_id: str
    product: str
    service: str
    hour: datetime
    active_customers: int
    requests: int
    transactions: int
    revenue: Optional[float]  # always None in Phase 1 — finance outcome deferred
    plan: str
    schema_version: str
    ingested_at: datetime


@dataclass
class DeploymentRow:
    """docs/phase0/schema-v1.md, `deployments` — grain: one row per deployment event."""

    organization_id: str
    service: str
    deployment_id: str
    released_at: datetime
    version: str
    changed_component: str
    actor: str
    schema_version: str
    ingested_at: datetime


@dataclass
class ResourceChangeRow:
    """docs/phase0/schema-v1.md, `resource_changes` — grain: one row per configuration event."""

    organization_id: str
    project_id: str
    resource_id: str
    changed_at: datetime
    change_type: str
    actor: str
    before_config: dict = field(default_factory=dict)
    after_config: dict = field(default_factory=dict)
    schema_version: str = "1"
    ingested_at: Optional[datetime] = None


@dataclass
class GroundTruthIncidentRow:
    """`ground_truth_incidents` — frozen in docs/phase0/incident-catalogue.md,
    extended with duration_hours/chronological_split in
    docs/phase1/incident-injection-spec.md.

    Read only by evaluation code (Phase 2's backtest harness, Phase 3's
    ranker scorer) — never joined into features_hourly or anything a
    detector reads.
    """

    ground_truth_id: str
    organization_id: str
    # "logging_regression" | "query_regression" | "idle_accelerator" | "autoscaling_error"
    incident_type: str
    affected_service_or_resource: str
    injected_at: datetime
    duration_hours: int
    expected_magnitude: dict  # shape depends on incident_type — see incidents/types.py
    expected_safe_remediation: str
    chronological_split: str  # "train" | "validation" | "test"
    simulator_seed: int
    resolved_incident_id: Optional[str] = None  # filled in later by Phase 2+
    schema_version: str = "1"
