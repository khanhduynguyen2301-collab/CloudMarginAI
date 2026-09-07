# CloudMargin AI — Threat Model & Governance (Phase 0)

Status: draft for review · Source: Blueprint v1.2, Section 14 ("Governance decisions that cannot be postponed")
Phase: 0 (Product specification) · Weeks 1–2

These are written down now and implemented in Phase 8 (Hardening). The rule from the source document: *"Technical controls cannot compensate for an undefined responsibility model."*

## Governance decisions

**Decision:** framed for a solo portfolio build — one real project owner, but `organization_id` is still enforced everywhere so RBAC and tenant-isolation logic are actually demonstrated rather than assumed away.

| Decision | This project's answer |
|---|---|
| Data ownership | Single project owner (you) is sole data controller. Plan on `org_demo` (your real GCP telemetry, from Phase 6 onward) plus 1–2 synthetic orgs used only for tenant-isolation tests |
| Retention — raw data | 30 days |
| Retention — aggregated data | 13 rolling months (12 + 1, so a full prior year is always available for year-over-year seasonal comparison) |
| Retention — audit data | Indefinite for the life of the project; deleted only when the demo GCP project itself is torn down |
| Access-review cadence | Reviewed at each phase-gate (9 times across the 24-week roadmap) rather than a continuous process — appropriate for a single-developer project; revisit if this ever has real users |
| Incident severity levels | Informational (below materiality threshold, no incident created) → Minor (single service, confidence < 0.7) → Major (confirmed cause, confidence ≥ 0.7) → Critical (breaches a reliability guardrail during validation) |
| Financial materiality threshold | **Operates on the simulator's modeled economy, not your real GCP bill.** Unexplained cost > $50/day *or* > 5% of expected cost, sustained ≥ 3 consecutive hours, at data-quality confidence ≥ 0.8. (Your actual infra spend stays in the $0–30/month range per Section 17 — it is never itself the materiality threshold) |
| Approval authorities | You approve every action personally, but each approval is attributed to one of the four demo personas (`finance_demo`, `finops_demo`, `eng_demo`, `admin_demo` — see `phase0-product-spec.md`) so separation-of-duty policy is actually exercised, not simulated away |
| Maximum action class allowed, this phase | **Advisory + Low risk only** — until Phase 7 (action validation) proves the closed loop works |

## Risk classes → approval policy

| Class | Examples | Default policy |
|---|---|---|
| Advisory | Ticket, report, owner notification | Agent may create automatically |
| Low risk | Schedule non-production environment shutdown | One authorized approval |
| Medium risk | Change retention or autoscaling minimum | Owner plus platform approval |
| High risk | Resize production database or change regional routing | Explicit senior engineering approval |
| Critical | Stop production service or broad rollback | Outside autonomous scope initially |

## Data-minimization rules

- Store aggregated log statistics whenever raw content is unnecessary.
- Do not ingest credentials, request bodies, or customer payloads into the analytical model by default.
- Use configurable retention for raw, aggregated, and audit data.
- Redact identifiers before sending context to Gemini unless explicitly needed and authorized.
- Document every external API and the data fields it can access.
- Concretely: redact `actor` (email/identity) and free-text fields in `resource_changes` before they reach Gemini; pass only structured, aggregated fields (counts, deltas, rates) by default — a named field only reaches the model when a specific evidence tool call requests it.

## Security controls to design against (documented now, built in Phase 8)

| Domain | Required control |
|---|---|
| Identity | Google identity or enterprise SSO; multi-factor authentication |
| Authorization | Organization-scoped RBAC plus resource and action policies |
| Service accounts | One identity per worker role; no shared broad credential |
| Secrets | Secret Manager, rotation, and access audit |
| Data isolation | Organization ID enforced in application, query, and storage boundaries |
| Encryption | Provider-managed encryption initially; customer-managed keys as later option |
| Network | Private service access where practical; restricted ingress and egress |
| Audit | Immutable record of evidence, approvals, actions, and policy decisions |

Everything above is "documented now, hardened in Phase 8" — except **Authorization** (org-scoped RBAC across the four demo personas): that's core product behavior for the website (Section 10, Phase 5), not a later hardening pass, and must work correctly from the first page that ships.

## Sign-off

- [x] All governance decisions above have an actual answer, not a placeholder
- [x] Maximum action class for this phase agreed (Advisory + Low risk only)
- [ ] Data-minimization rules enforced in the agent's tool layer (Phase 4 — not yet, correctly)
