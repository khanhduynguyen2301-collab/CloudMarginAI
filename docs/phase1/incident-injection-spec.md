# CloudMargin AI — Incident Injection Spec (Phase 1)

Status: draft for review · Source: Blueprint v1.2, Section 8 & Appendix E; `docs/phase0-incident-catalogue.md`
Phase: 1 (Data simulator) · Weeks 3–5

`phase0-incident-catalogue.md` named the four incident types and what ground truth to record. This doc makes each one an executable procedure against the generators defined in `phase1-workload-cost-model.md`, with the counts, timing, and non-overlap rules Phase 2 and Phase 3's evaluations depend on.

## Injection engine contract

**Decision:** every incident is injected by mutating exactly one named parameter of the clean generators for a bounded window — never by hand-editing output rows after the fact. This keeps the causal chain (parameter change → workload/metric effect → cost effect) real, which is what makes root-cause ranking in Phase 3 a legitimate test rather than a lookup.

```
inject(incident_type, service/resource, start_ts, duration_hours, magnitude) -> {
    mutate the relevant generator parameter for [start_ts, start_ts + duration_hours)
    optionally apply a remediation event at end_ts that reverts the parameter
    write one row to incident_ground_truth
}
```

## The four incident types, operationalized

### 1. Logging regression — `ingestion-worker`
- **Mutated parameter:** log verbosity multiplier on `logging.ingested-gb` usage.
- **Magnitude:** **3.5×–6× baseline log bytes**, traffic (`requests`) untouched.
- **Duration:** persistent until remediated — **72–168 hours** (unremediated in-window, since Phase 7's remediation logic doesn't exist yet; the simulator marks `remediated_at = null` for Phase 1 purposes).
- **Ground truth recorded:** `injection_ts`, `service = ingestion-worker`, `log_byte_multiplier`.

### 2. Query regression — `checkout-api`
- **Mutated parameter:** `db.cpu-hour` usage-per-request multiplier and `latency_p50/p99` congestion factor.
- **Magnitude:** **1.8×–3× queries/request**, **1.5×–2.5× p99 latency**, proportional `db.cpu-hour` and cost rise.
- **Duration:** **48–120 hours.**
- **Ground truth recorded:** `injection_ts`, `service = checkout-api`, `latency_delta_pct`, `cost_delta_pct`.

### 3. Idle accelerator — `ml-training-job`
- **Mutated parameter:** the job's fixed schedule is forced "active" with GPU utilization capped near-zero instead of returning to idle/off.
- **Magnitude:** **GPU utilization held below 5%** while `compute.gpu-hour` usage continues at its normal active rate.
- **Duration:** **96–240 hours** (idle-accelerator incidents tend to linger longest before anyone notices, which is the point of the incident).
- **Ground truth recorded:** `injection_ts`, `resource = ml-training-job GPU pool`, `idle_cost_rate`.

### 4. Autoscaling error — `recommendation-api`
- **Mutated parameter:** `min_replicas` policy bound.
- **Magnitude:** **min_replicas raised 3×–5×** its normal floor, decoupling `instance_count` from `requests` (utilization falls while instance count stays elevated).
- **Duration:** **48–96 hours.**
- **Ground truth recorded:** `injection_ts`, `service = recommendation-api`, `instance_count_delta`.

## Count and placement across the 90 days

**Decision:** **10 total incidents**, 2–3 per type, distributed across the chronological splits from `phase1-simulator-architecture.md` so every split has genuine incident coverage without contaminating the others:

| Split (days) | Incidents placed | Purpose |
|---|---|---|
| Train (1–60) | 6 (1–2 per type) | Lets Phase 2 learn what a real regression's signature looks like before scoring |
| Validation (61–75) | 2 (spread across types) | Threshold/interval calibration |
| Test / held-out (76–90) | 2, **including exactly one logging regression** | M2's milestone — "flag a held-out logging regression with calibrated bounds" — is satisfied directly by this row |

**Non-overlap rule:** no two incidents share overlapping time windows on the same service, and no incident window crosses a split boundary. A single service may carry more than one incident type across the 90 days, but never two active at once — concurrent incidents on one service would make Phase 3's "true cause in top 3" evaluation ambiguous by construction, which the blueprint's own reasoning for choosing these four incident types (clean, close-to-single-cause signatures) explicitly rules out.

## Ground-truth schema — `ground_truth_incidents` (already defined in `phase0-incident-catalogue.md`; this section operationalizes it)

**Correction from the original draft of this doc:** this table is not a new v1.1 addition. `phase0-incident-catalogue.md` already froze it as `ground_truth_incidents` and signed it off in Phase 0. The table below is that same table, with two fields this doc's injection engine needs layered on additively (`duration_hours`, `chronological_split`) — not a rename or a rewrite. The original draft here called it `incident_ground_truth` with a flat `magnitude_field`/`magnitude_value` pair; that's fixed below, because a flat pair can't hold query-regression's two ground-truth values (`latency_delta_pct` and `cost_delta_pct`) at once — `phase0-incident-catalogue.md`'s `expected_magnitude` JSON field doesn't have that problem, so this doc now matches it exactly.

| Field | Type | Notes |
|---|---|---|
| ground_truth_id | TEXT PK | e.g. `GT-0007` |
| organization_id | TEXT | `org_demo` |
| incident_type | TEXT | `logging_regression` \| `query_regression` \| `idle_accelerator` \| `autoscaling_error` |
| affected_service_or_resource | TEXT | matches `resource_metrics_hourly`/`billing_hourly` join key |
| injected_at | TIMESTAMPTZ | exact start — `t=0` for MTTD (per `phase0-incident-catalogue.md`) |
| duration_hours | INT64 | additive field this doc needs; not in the original Phase 0 table |
| expected_magnitude | JSON | shape depends on `incident_type` — see the JSON per type below |
| expected_safe_remediation | TEXT | one-line description from `phase0-incident-catalogue.md` (restore logging level / rollback query change / stop-or-schedule / restore scaling policy) |
| chronological_split | TEXT | `train` \| `validation` \| `test` — additive field this doc needs |
| simulator_seed | INT | the deterministic seed that generated this scenario, per `phase0-incident-catalogue.md` |
| resolved_incident_id | TEXT | nullable FK → `incidents.incident_id` — filled in once a detector (Phase 2+) matches this ground truth; required for MTTD/MTTR-cause/Top-3/MRR to be computed by join rather than eyeballed (`phase0-incident-catalogue.md`) |
| schema_version | STRING | |

`expected_magnitude` per incident type: `{"log_byte_multiplier": <3.5–6>}` for logging_regression; `{"latency_delta_pct": <val>, "cost_delta_pct": <val>}` for query_regression; `{"idle_cost_rate": <val>}` for idle_accelerator; `{"instance_count_delta": <val>}` for autoscaling_error.

This table is read only by evaluation code (Phase 2's backtest harness, Phase 3's ranker scorer) — it must never be joined into `features_hourly` or any table the detector itself reads, or the evaluation becomes circular.

## Held-for-later incidents

Traffic abuse and storage/log-retention remain out of scope for Phase 1, unchanged from `phase0-incident-catalogue.md`'s reasoning: both interact with demand/business signals in ways that would need the demand model above to be more sophisticated first.

## Sign-off

- [ ] Magnitude ranges per incident type confirmed as clearly separable from the noise levels set in `phase1-workload-cost-model.md`
- [ ] 10-incident count and train/validation/test split confirmed as sufficient for Phase 2/3 evaluation (not so sparse that metrics are noisy, not so dense that "incident" stops being an exceptional state)
- [ ] Non-overlap rule accepted as a hard constraint enforced by `simulator/validate.py`
- [ ] `ground_truth_incidents` schema (from `phase0-incident-catalogue.md`, extended here with `duration_hours` and `chronological_split`) accepted as sufficient for this injection engine
- [ ] Approved to proceed to `phase1-validation-plan.md`
