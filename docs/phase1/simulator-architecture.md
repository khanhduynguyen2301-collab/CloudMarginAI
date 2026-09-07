# CloudMargin AI — Simulator Architecture (Phase 1)

Status: draft for review · Source: CloudMargin AI Product & Technical Blueprint v1.2, Sections 6–8, Appendix C, E
Phase: 1 (Data simulator) · Weeks 3–5

This is the one-page architecture Phase 1 exists to produce. Nothing in Phase 2 (analytical spine) should require a decision that isn't already settled here or in the Phase 0 docs it builds on.

## Role in the roadmap

Roadmap row: **1. Data simulator | Weeks 3–5 | Seasonal workloads, billing model, labeled incident injection | Exit gate: reproducible dataset with ground truth.**

Milestone M1 (Appendix, Section 15): *generate 90 days of workload, cost, deployment, and incident history with deterministic seeds.* Appendix E's immediate next action for this phase: *implement a deterministic 90-day workload and billing simulator.* Everything below exists to satisfy those two sentences and nothing more — Phase 1 does not touch Gemini, the agent, the website, or real GCP connectors.

## Non-goals for this phase

- No live GCP calls of any kind — the simulator is a self-contained generator, not a connector.
- No forecasting, anomaly detection, or root-cause logic — Phase 2 and Phase 3 consume this dataset, they are not built here.
- No website or API surface — output is files/tables, inspected via notebook or CLI.
- No AWS/Azure shapes — Google Cloud SKU and metric shapes only, per Phase 0's single-cloud principle.

## Repository layout

**Decision:** the `simulator/` package (Appendix C: *workloads, cloud-cost model, incident injection, label generation*) is organized as four sibling modules plus a CLI, so each piece can be unit-tested and reused independently by Phase 2's backtests:

| Path | Responsibility |
|---|---|
| `simulator/topology.py` | Defines the fixed org/project/service/region graph (see below) |
| `simulator/workloads/` | Demand, capacity, efficiency, reliability generators (feeds `resource_metrics_hourly`, `application_activity_hourly`) |
| `simulator/cost_model/` | Unit-price table and usage→cost translation (feeds `billing_hourly`) |
| `simulator/changes/` | Deployment cadence and background config-change generator (feeds `deployments`, `resource_changes`) |
| `simulator/incidents/` | Incident injection engine — the one place allowed to perturb workload/cost/change generators (see `phase1-incident-injection-spec.md`) |
| `simulator/labels/` | Ground-truth writer — one row per injected incident, independent of what any detector later infers |
| `simulator/validate.py` | Schema, uniqueness, sign, and reproducibility checks (see `phase1-validation-plan.md`) |
| `simulator/cli.py` | `simulate --seed --start-date --days --config` entry point |

## Topology decision

Phase 0 fixed four RBAC personas under a single organization (`org_demo`). The simulator's job is to make that organization's infrastructure look real enough to detect incidents in, not to model many tenants — multi-tenant scale is out of scope until Phase 6 connects a real org.

**Decision — one organization, fixed topology for v1:**

| Level | Value |
|---|---|
| Organization | `org_demo` (single organization, matches the four demo accounts) |
| Projects | `prod`, `staging` (2) |
| Services | `checkout-api`, `recommendation-api`, `ingestion-worker`, `billing-worker`, `ml-training-job`, `web-frontend` (6, in `prod`; `staging` mirrors a 2-service subset for noise/contrast) |
| Regions | `us-central1`, `us-east1`, `europe-west1` (3) |
| Resource kind per service | Each service maps to one dominant resource shape: request-serving (checkout-api, recommendation-api, web-frontend), worker/queue-driven (ingestion-worker, billing-worker), or batch/accelerator (ml-training-job — this is the idle-accelerator incident's target) |

This gives every `engineering_owner` demo account a plausible `owned_services` slice (Phase 0's `user_roles.owned_services`) and gives the incident catalogue's four incident types a natural home each: logging regression → `ingestion-worker`; query regression → `checkout-api`; idle accelerator → `ml-training-job`; autoscaling error → `recommendation-api`.

## Time range and chronological structure

**Decision:** 90 days total, hourly grain (matches `phase0-schema-v1.md`'s `*_hourly` tables), split chronologically — never randomly, per the blueprint's experimental-design rule:

| Window | Days | Use |
|---|---|---|
| Train | 1–60 | Baseline/model fitting in Phase 2 |
| Validation | 61–75 | Threshold and interval calibration |
| Test (held out) | 76–90 | Backtest scoring; M2's held-out logging regression lives here |

Incident windows are assigned to exactly one split and never straddle a split boundary (detailed in `phase1-incident-injection-spec.md`) — this is what makes the Phase 2 exit gate ("detection beats baselines on held-out incidents") a fair test.

## Reproducibility strategy

**Decision:** a single master seed produces every derived seed, so `simulate --seed 42` is byte-for-byte reproducible end to end:

- `seed_workload = hash(master_seed, "workload")`
- `seed_cost_noise = hash(master_seed, "cost_noise")`
- `seed_incidents = hash(master_seed, "incidents")`
- `seed_changes = hash(master_seed, "changes")`

Each module receives only its own derived seed and its own `numpy.random.Generator` instance — no shared global RNG state, so adding a module later can't silently shift another module's random stream. The master seed and all derived seeds are written into the run's manifest (see below) so a dataset can always be traced back to the exact generator call that produced it.

## Output targets

The simulator writes directly into the five analytical tables `phase0-schema-v1.md` already froze — it does not invent a parallel schema:

`billing_hourly`, `resource_metrics_hourly`, `application_activity_hourly`, `deployments`, `resource_changes`.

`source` is always `estimated` in `billing_hourly` (no real billing export exists yet) and `is_reconciled` is always `false`. `application_activity_hourly.revenue` is left null, consistent with Phase 0's decision to defer the finance/margin outcome.

**Ground truth table:** `phase0-incident-catalogue.md` already froze `ground_truth_incidents` for exactly this purpose — owned by `simulator/labels/`, operationalized in `phase1-incident-injection-spec.md`. It is intentionally separate from the production `incidents` table: `incidents` is what the detector *claims* happened; `ground_truth_incidents` is what the simulator *knows* it injected. Phase 2's evaluation code is the only consumer allowed to join the two.

Every output row still carries `organization_id` and `schema_version`, per the schema doc's blanket rule.

## Run manifest

**Decision:** every simulator run emits a `manifest.json` alongside its data: master seed, derived seeds, date range, split boundaries, topology version, cost-model version, and the list of injected incidents by ID (without their ground-truth magnitude, which lives in `ground_truth_incidents` so it isn't accidentally leaked into a features table). This is what lets Phase 2's backtests and Appendix D's "definition of done" claim ("the simulator reproduces labeled scenarios from a fixed random seed") be checked mechanically instead of by inspection.

## Sign-off

- [ ] Topology (2 projects, 6+2 services, 3 regions) confirmed as sufficient contrast for the four incident types
- [ ] 90-day / 60-15-15 chronological split confirmed against Phase 2's backtest needs
- [ ] `ground_truth_incidents` (already frozen in `phase0-incident-catalogue.md`) accepted as separate from `incidents`, no new table needed
- [ ] Approved to proceed to `phase1-workload-cost-model.md` and `phase1-incident-injection-spec.md`
