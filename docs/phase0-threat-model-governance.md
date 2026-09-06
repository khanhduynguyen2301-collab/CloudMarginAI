# CloudMargin AI — Threat Model & Governance (Phase 0)

Status: draft for review · Source: Blueprint v1.2, Section 14 ("Governance decisions that cannot be postponed")
Phase: 0 (Product specification) · Weeks 1–2

These are written down now and implemented in Phase 8 (Hardening). The rule from the source document: *"Technical controls cannot compensate for an undefined responsibility model."*

## Governance decisions

Fill in each before connecting any real organization's data:

| Decision | This project's answer |
|---|---|
| Data ownership | *(who owns billing / telemetry / audit data per organization — for a solo portfolio build, this is the project owner's own GCP org)* |
| Retention — raw data | *(e.g. 30 days raw telemetry)* |
| Retention — aggregated data | *(e.g. 13 months, to support seasonal forecasting)* |
| Retention — audit data | *(e.g. indefinite, or per compliance needs)* |
| Access-review cadence | *(e.g. reviewed at the start of each phase)* |
| Incident severity levels | *(map to the risk classes below)* |
| Financial materiality threshold | *(the $ amount + duration that turns an anomaly into an incident — e.g. cost exceeds the upper prediction bound for ≥3 consecutive hours AND unexplained cost exceeds a configurable amount)* |
| Approval authorities | *(who can approve which risk class — see table below)* |
| Maximum action class allowed, this phase | **Advisory + Low risk only** (recommended default until the closed-loop validation phase proves itself) |

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

## Sign-off

- [ ] All governance decisions above have an actual answer, not a placeholder
- [ ] Maximum action class for this phase agreed
- [ ] Data-minimization rules will be enforced in the agent's tool layer (Phase 4)
