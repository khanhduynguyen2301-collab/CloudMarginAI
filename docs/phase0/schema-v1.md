# CloudMargin AI — Schema Definitions v1 (Phase 0)

Status: draft for review · Source: Blueprint v1.2, Sections 5–7 & Appendices A–B
Phase: 0 (Product specification) · Weeks 1–2

Every table below carries `organization_id` and `schema_version`. BigQuery holds analytical facts; PostgreSQL holds transactional workflow state (incidents, evidence, recommendations) — the two are never swapped (BigQuery is not the transactional backend for approvals; PostgreSQL is not the warehouse for billing/telemetry analysis).

## Identifier grain

The shared join key across every analytical table: **organization → project → service → region → hour.**

## Canonical BigQuery tables

### `billing_hourly` — grain: project-service-SKU-region-hour
| Field | Type | Notes |
|---|---|---|
| organization_id | STRING | |
| project_id | STRING | |
| service | STRING | normalized service name |
| sku | STRING | |
| region | STRING | |
| hour | TIMESTAMP | |
| usage_amount | FLOAT64 | |
| usage_unit | STRING | |
| credits | FLOAT64 | |
| effective_cost | FLOAT64 | |
| currency | STRING | |
| is_reconciled | BOOL | false until finalized billing data arrives |
| source | STRING | `estimated` \| `billing_export` |
| schema_version | STRING | |
| ingested_at | TIMESTAMP | |

### `resource_metrics_hourly` — grain: resource-hour
| Field | Type | Notes |
|---|---|---|
| organization_id, project_id, service, resource_id, region | STRING | |
| hour | TIMESTAMP | |
| cpu_utilization, memory_utilization, gpu_utilization | FLOAT64 | |
| instance_count | INT64 | |
| request_count, error_count | INT64 | |
| latency_p50_ms, latency_p99_ms | FLOAT64 | |
| schema_version, ingested_at | | |

### `application_activity_hourly` — grain: product-service-hour
| Field | Type | Notes |
|---|---|---|
| organization_id, product, service | STRING | |
| hour | TIMESTAMP | |
| active_customers, requests, transactions | INT64 | |
| revenue | FLOAT64 | nullable — only when Business KPI API is connected |
| plan | STRING | |
| schema_version, ingested_at | | |

### `deployments` — grain: deployment event
| Field | Type | Notes |
|---|---|---|
| organization_id, service, deployment_id | STRING | |
| released_at | TIMESTAMP | |
| version, changed_component, actor | STRING | |
| schema_version, ingested_at | | |

### `resource_changes` — grain: configuration event
| Field | Type | Notes |
|---|---|---|
| organization_id, project_id, resource_id | STRING | |
| changed_at | TIMESTAMP | |
| change_type, actor | STRING | |
| before_config, after_config | JSON | |
| schema_version, ingested_at | | |

### `features_hourly` — grain: service-hour
| Field | Type | Notes |
|---|---|---|
| organization_id, project_id, service, region | STRING | |
| hour | TIMESTAMP | |
| expected_cost, expected_cost_lower, expected_cost_upper, residual | FLOAT64 | |
| rolling_mean_7d | FLOAT64 | |
| lag_1h_cost, lag_24h_cost, lag_168h_cost | FLOAT64 | |
| hours_since_deployment | INT64 | |
| model_version, feature_version | STRING | |
| schema_version, computed_at | | |

## Transactional (PostgreSQL) tables

### `user_roles` (added per Phase 0 personas decision — separate RBAC-enforced demo accounts, not a role-switcher)
| Field | Type | Notes |
|---|---|---|
| user_id | TEXT PK | |
| organization_id | TEXT | |
| role | TEXT | `finance` \| `finops_analyst` \| `engineering_owner` \| `admin` |
| owned_services | TEXT[] | nullable; scopes an `engineering_owner` to their services |
| created_at | TIMESTAMPTZ | |

This table is read by the API layer on every request — role boundaries are enforced server-side, not just hidden in the UI (Section 11's endpoint-authorization table: read APIs need viewer-or-higher, approval commands need approver role + policy match, administration needs organization administrator).

### `incidents` (Appendix A field set, extended — see review notes below)
| Field | Type | Example |
|---|---|---|
| incident_id | TEXT PK | `INC-2026-0014` |
| organization_id | TEXT | `org_demo` |
| correlation_id | TEXT | ties every event/log/trace touching this incident together (Section 11 transition invariant) |
| affected_service | TEXT | `recommendation-api` |
| detected_at | TIMESTAMPTZ | |
| expected_cost | NUMERIC | with interval bounds |
| actual_cost | NUMERIC | |
| unexplained_cost | NUMERIC | |
| probable_cause | TEXT | top hypothesis, denormalized for quick display |
| confidence | FLOAT | 0–1, confidence in `probable_cause` |
| hypotheses | JSONB | ranked list: `[{cause, confidence, evidence_for: [evidence_id], evidence_against: [evidence_id]}]` — the investigation policy requires ≥2 for material incidents |
| unanswered_questions | TEXT[] | what the agent couldn't resolve before stopping (part of the output contract) |
| proposed_action | TEXT | |
| status | TEXT | `detected` → `investigating` → `awaiting_approval` → `executing` → `validating` → `resolved` \| `reopened` |
| model_version, feature_version | TEXT | feature_version + affected_service + detected_at's hour together identify the exact `features_hourly` row used — the "feature snapshot" the transition invariant requires |
| created_at, updated_at | TIMESTAMPTZ | |

### `investigation_evidence` (Appendix B pattern: evidence → observed result → interpretation)
| Field | Type | Notes |
|---|---|---|
| evidence_id | TEXT PK | |
| incident_id | TEXT FK → incidents | |
| tool_name | TEXT | one of the agent's constrained tools (`get_cost_breakdown`, `compare_periods`, `get_utilization`, `find_recent_changes`, `inspect_log_statistics`, `trace_dependencies`, `estimate_savings`) |
| input | JSON | |
| observed_result | JSON | |
| interpretation | TEXT | |
| source_reference | TEXT | query ID / dashboard link, for reproducibility |
| created_at | TIMESTAMPTZ | |

### `recommendations` (extended — see review notes below)
| Field | Type | Notes |
|---|---|---|
| recommendation_id | TEXT PK | |
| incident_id | TEXT FK → incidents | |
| idempotency_key | TEXT UNIQUE | required so a duplicate `action.approved` message can never create a duplicate remediation (Appendix D) |
| target_project, target_resource | TEXT | |
| current_config, proposed_config | JSON | |
| evidence_ids | TEXT[] | |
| expected_savings_low, expected_savings_high | NUMERIC | |
| savings_time_horizon | TEXT | e.g. `"30 days post-action"` — the recommendation contract requires a horizon, not just a range |
| risk_class | TEXT | `advisory` \| `low` \| `medium` \| `high` \| `critical` |
| risk_description | TEXT | the actual reliability/business risk, not just the class label |
| required_approvers | TEXT[] | |
| rollback_trigger, rollback_procedure | TEXT | |
| validation_metrics | TEXT[] | |
| validation_window_hours | INT | observation window before validating a savings claim |
| status | TEXT | `proposed` → `approved` \| `rejected` → `executed` → `validated` |
| decided_by, decided_at | TEXT, TIMESTAMPTZ | |
| executed_at, validated_at | TIMESTAMPTZ | nullable until each stage happens — needed to actually measure MTTR/validation-window metrics from Part 1 |

### `outbox_events` (Section 11's transactional-outbox pattern)
| Field | Type | Notes |
|---|---|---|
| event_id | TEXT PK | |
| aggregate_type | TEXT | `incident` \| `recommendation` |
| aggregate_id | TEXT | the incident_id or recommendation_id this event is about |
| event_type | TEXT | e.g. `anomaly.detected`, `action.approved`, `validation.completed` |
| payload | JSON | |
| correlation_id | TEXT | |
| occurred_at | TIMESTAMPTZ | written in the *same transaction* as the incident/recommendation row it describes |
| published_at | TIMESTAMPTZ | nullable — set once the publisher worker forwards it to Pub/Sub |

Why this table exists: the doc's own critical consistency rule is that a database commit and an event publication can't be treated as one atomic operation. Without an outbox row written in the same transaction as the incident/approval change, "never publish `action.approved` before the approval transaction commits" (Section 11) has no table to enforce it against.

## Versioning

Every table above carries `schema_version`. No table is scored or read by a downstream stage before it passes the pipeline's validate step (schema, nulls, duplication, event time, cost signs, unit consistency).

## Review notes: agent memory-contract check

Checked `incidents`, `investigation_evidence`, and `recommendations` against Section 9's agent output contract (incident summary, verified facts, *hypotheses*, evidence for/against each, confidence, financial impact, proposed action, risk, approval requirement, rollback condition, *unanswered questions*), Section 11's incident-transition invariants, and Appendix D's "duplicate messages cannot create duplicate remediations." Three real gaps found and fixed above, not cosmetic ones:

1. `incidents` had a single `probable_cause` + `confidence` but nowhere to put the *ranked alternative hypotheses* the investigation policy requires (≥2 for material incidents) — added `hypotheses` (JSONB) and `unanswered_questions`.
2. `incidents` had no `correlation_id`, which Section 11 lists as a required field on the very first status transition (detected → investigating) — added it.
3. `recommendations` had no idempotency key and no `executed_at`/`validated_at` timestamps — meaning nothing actually enforced "duplicate messages cannot create duplicate remediations," and MTTR/validation-window metrics from the product spec had no timestamps to compute from. Added `idempotency_key`, `executed_at`, `validated_at`, plus `savings_time_horizon`, `risk_description`, and `validation_window_hours` to match the full recommendation contract in Section 12.
4. Added `outbox_events` — the transactional-outbox pattern Section 11 requires has to write to *something* in the same transaction as an incident/recommendation change; there was no table for it.

## Sign-off

- [ ] Field types confirmed against actual GCP export schemas (billing export, Cloud Monitoring, Cloud Asset Inventory) once connectors are built in Phase 6
- [x] `incidents`, `investigation_evidence`, `recommendations` reviewed as the agent's memory contract — 4 gaps found and closed (see review notes)
- [x] v1 frozen — changes after this point are new versions, not edits
