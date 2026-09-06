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

### `incidents` (Appendix A field set)
| Field | Type | Example |
|---|---|---|
| incident_id | TEXT PK | `INC-2026-0014` |
| organization_id | TEXT | `org_demo` |
| affected_service | TEXT | `recommendation-api` |
| detected_at | TIMESTAMPTZ | |
| expected_cost | NUMERIC | with interval bounds |
| actual_cost | NUMERIC | |
| unexplained_cost | NUMERIC | |
| probable_cause | TEXT | |
| confidence | FLOAT | 0–1 |
| proposed_action | TEXT | |
| status | TEXT | `detected` → `investigating` → `awaiting_approval` → `executing` → `validating` → `resolved` \| `reopened` |
| model_version, feature_version | TEXT | |
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

### `recommendations`
| Field | Type | Notes |
|---|---|---|
| recommendation_id | TEXT PK | |
| incident_id | TEXT FK → incidents | |
| target_project, target_resource | TEXT | |
| current_config, proposed_config | JSON | |
| evidence_ids | TEXT[] | |
| expected_savings_low, expected_savings_high | NUMERIC | |
| risk_class | TEXT | `advisory` \| `low` \| `medium` \| `high` \| `critical` |
| required_approvers | TEXT[] | |
| rollback_trigger, rollback_procedure | TEXT | |
| validation_metrics | TEXT[] | |
| status | TEXT | `proposed` → `approved` \| `rejected` → `executed` → `validated` |
| decided_by, decided_at | TEXT, TIMESTAMPTZ | |

## Versioning

Every table above carries `schema_version`. No table is scored or read by a downstream stage before it passes the pipeline's validate step (schema, nulls, duplication, event time, cost signs, unit consistency).

## Sign-off

- [ ] Field types confirmed against actual GCP export schemas (billing export, Cloud Monitoring, Cloud Asset Inventory) once connectors are built in Phase 6
- [ ] `incidents`, `investigation_evidence`, `recommendations` reviewed as the agent's memory contract (Phase 4 depends on this)
- [ ] v1 frozen — changes after this point are new versions, not edits
