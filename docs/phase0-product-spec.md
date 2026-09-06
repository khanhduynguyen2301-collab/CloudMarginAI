# CloudMargin AI — Product Specification (Phase 0)

Status: draft for review · Source: CloudMargin AI Product & Technical Blueprint v1.2, Sections 1–4
Phase: 0 (Product specification) · Weeks 1–2

This is the one-page spec Phase 0 exists to produce. Nothing in Phase 1 (data simulator) should require a decision that isn't already settled here.

## Thesis

CloudMargin AI is a multi-user platform that continuously examines cloud spending, product usage, engineering telemetry, and deployment history. It detects financially material cost anomalies, investigates their probable causes, quantifies avoidable spending, proposes a controlled response, and verifies whether the response reduced cost without harming reliability.

**Product thesis:** a cloud-cost platform becomes materially more useful when it progresses from reporting to evidence-based investigation, controlled action, and post-action validation.

## Business outcomes

**Decision:** for a synthetic-data solo build, trimmed to what the simulator can actually prove. The finance/margin outcome moves to "deferred" — it depends on real business KPI data (revenue, plan, customer count) that Section 6 already marks as optional (`application_activity_hourly.revenue` is nullable "only when Business KPI API is connected").

### Provable now, with synthetic + injected-incident data

| # | Outcome | Metric(s) | How it's computed | Target |
|---|---|---|---|---|
| 1 | Reduce mean time to detect a financially material anomaly | **MTTD** (Mean Time to Detect) | Hours from the injected regression's timestamp to `incidents.detected_at` | Set from Phase 2 backtests (M2 milestone: flag a held-out regression with calibrated bounds) |
| 2 | Reduce mean time to identify a probable root cause | **MTTR-cause**, Top-1 / Top-3 accuracy, MRR | Hours from `detected_at` to the first ranked hypothesis list; % of incidents where the true injected cause lands in the top 3 candidates | True cause in top 3 for confirmed incidents (M3 milestone) |
| 3 | Lower avoidable spend without breaking reliability | **Avoidable cost detected** ($), **reliability guardrail violations** (count) | Sum of `unexplained_cost` across confirmed incidents; count of latency/error/availability threshold breaches observed during each validation window | Guardrail violations = 0, always — a constraint, not something to optimize |
| 4 | Give engineering evidence + a rollback-aware plan | **Evidence-grounding rate**, **rollback completeness** | % of recommendation claims that cite an `evidence_id`; % of `recommendations` rows with both `rollback_trigger` and `rollback_procedure` populated | Rollback completeness = 100% — a recommendation without one doesn't ship |
| 5 | Measure realized savings, not just proposed savings | **Estimated vs. realized savings** ($), acceptance / rejection rate | `expected_savings_low/high` at approval time vs. the post-validation counterfactual; analyst decision recorded per recommendation | Always reported as a range, never a point estimate |

### Deferred until real business data connects
6. Show finance how operational incidents affect gross margin and forecast accuracy. Metric would be **gross-margin delta attributable to confirmed incidents**, once `application_activity_hourly.revenue` is populated. *(needs a live Business KPI API / real revenue — not available in a synthetic-only build; revisit once Phase 6 connects real data, or if you get access to representative business metrics)*

## Scope (v1)

| Included in v1 | Deferred |
|---|---|
| Google Cloud billing, monitoring, logging, asset, and audit connectors | AWS and Azure connectors |
| Hourly and daily cost analysis | Sub-minute cost estimation |
| Forecasting, anomaly detection, change points, and cause ranking | Fully autonomous production remediation |
| Multi-user website with roles and approvals | Complex enterprise procurement and invoicing |
| Four deeply evaluated incident families | An unrestricted catalogue of optimization rules |

## Product principles (non-negotiable constraints)

1. **Evidence before explanation.** Every conclusion links to a query result, metric, event, or model output.
2. **Read-only by default.** Investigation permissions are separated from remediation permissions.
3. **Uncertainty is visible.** The system presents confidence and alternative explanations rather than pretending correlation proves causality.
4. **Financial materiality matters.** Small technical anomalies do not automatically become incidents.
5. **Reliability is a constraint, not an afterthought.** Savings are rejected if they create unacceptable latency, errors, or availability risk.
6. **Human responsibility remains explicit.** Approval does not disappear behind an autonomous-agent label.
7. **Single-cloud excellence before multi-cloud breadth.** Google Cloud is the first complete implementation.

## Non-goals

- Replacing a cloud provider's native billing system.
- Guaranteeing causal proof from observational telemetry alone.
- Allowing an LLM to execute arbitrary shell commands or unrestricted SQL.
- Optimizing exclusively for the lowest bill regardless of reliability or growth.
- Training a large language model from scratch.
- Using complexity or microservice count as a measure of system quality.

## Personas & core workflows

**Decision:** four separate seeded demo accounts, with RBAC actually enforced between them — not a single account with a role-switcher. This makes the roles table a load-bearing part of the schema starting now, not a cosmetic layer added later at the website phase.

| Persona | Demo account | Needs | Can't see |
|---|---|---|---|
| Finance executive | `finance_demo` | Budget variance, monthly forecast, margin impact, decisions awaiting approval | Investigation workspace internals, other orgs' data |
| FinOps analyst | `finops_demo` | Granular cost allocation, anomaly triage, savings estimates, evidence that recommendations worked | Admin/connector configuration |
| Engineering owner | `eng_demo` | Technical context, service ownership, recent changes, reliability risk, reproducible query trail | Approval-authority actions outside their owned services |
| Platform administrator | `admin_demo` | Connector health, role management, policy controls, auditability, data-retention settings | — (broadest visibility, but still organization-scoped) |

Consequence for later phases: Phase 0's schema needs a `roles` concept attached to `organization_id` (see `phase0-schema-v1.md` — add a `user_roles` table there before Phase 5), and Phase 5 (website) must enforce these boundaries in the API layer, not just hide UI elements client-side.

Core workflows: continuous monitoring → incident investigation → approval → validation → learning → executive review.

## Sign-off

- [x] Reviewed against scope table, principles, and non-goals
- [x] Personas confirmed as four separate RBAC-enforced demo accounts (not a role-switcher)
- [x] Business outcomes trimmed to 5 named, measurable metrics (MTTD; MTTR-cause + Top-3/MRR; avoidable cost + guardrail violations; evidence-grounding + rollback completeness; estimated vs. realized savings) — finance/margin outcome deferred
- [ ] Approved to proceed to Phase 1 (data simulator)
