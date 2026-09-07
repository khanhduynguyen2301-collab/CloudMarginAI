# CloudMargin AI — Simulator Validation & Exit Criteria (Phase 1)

Status: draft for review · Source: Blueprint v1.2, Section 7 (data-quality gates), Section 16 (testing pyramid), Section 15 (roadmap exit gate), Appendix D
Phase: 1 (Data simulator) · Weeks 3–5

The roadmap's exit gate for this phase is one sentence — "reproducible dataset with ground truth" — and Appendix D's definition of done adds "the simulator reproduces labeled scenarios from a fixed random seed." This doc turns both into checks that pass or fail mechanically, so Phase 2 doesn't inherit a dataset nobody actually verified.

## Data-quality gates (applied to simulator output, not live connectors)

`phase0-product-spec.md`'s data-quality gates were written for real connectors; the subset that applies to a generated dataset:

| Gate | Check | Failure response |
|---|---|---|
| Schema | Every output row matches its table's field set and types in `phase0-schema-v1.md` / `ground_truth_incidents` (`phase0-incident-catalogue.md`) | Fail the run; no partial dataset is published |
| Uniqueness | No duplicate rows on each table's declared grain (e.g. project-service-SKU-region-hour for `billing_hourly`) | Fail the run |
| Nulls | No unexpected nulls outside the documented nullable fields (`revenue`, `remediated_at`) | Fail the run |
| Cost signs | `usage_amount`, `effective_cost`, `credits` are all ≥ 0 | Fail the run |
| Unit consistency | Every SKU's `usage_unit` matches the table in `phase1-workload-cost-model.md`; no mixed units within a SKU | Fail the run |
| Lineage | Every row carries `organization_id` and `schema_version`; every run carries a `manifest.json` (seed, split boundaries, incident list) | Fail the run |

These run as part of `simulator/validate.py` (declared in `phase1-simulator-architecture.md`) and block the CLI from writing a "done" marker if any gate fails — no dataset without a green validation run is usable downstream.

## Reproducibility test

**Decision — the concrete test behind "reproduces labeled scenarios from a fixed random seed":**

1. Run `simulate --seed 42` twice into separate output directories.
2. Hash every output table (row-order-independent hash, since generation order isn't a guaranteed contract) and compare.
3. Compare the two `manifest.json` incident lists field-for-field.
4. Pass requires byte-identical hashes and identical incident ground truth — not "statistically similar."

Separately, run `simulate --seed 7` and confirm the output *differs* from seed 42 in both cost totals and incident placement — a generator that produces the same output regardless of seed would pass step 1–4 for the wrong reason.

## Causal sanity checks (does the model mean what it says)

Beyond schema validity, the dataset has to actually exhibit what `phase0-incident-catalogue.md` and `phase1-incident-injection-spec.md` claim it will:

- For every `logging_regression` ground-truth row: ingested-log-bytes in its window are within the declared `log_byte_multiplier` range of the pre-injection baseline, and `requests` in the same window is within normal seasonal variation (i.e., the "traffic stays mostly flat" signature is actually true in the data).
- For every `query_regression` row: latency and `db.cpu-hour` both rise together in-window.
- For every `idle_accelerator` row: GPU utilization is below 5% while `compute.gpu-hour` usage is unchanged from baseline.
- For every `autoscaling_error` row: `instance_count` rises while `cpu_utilization` falls, in-window.
- Outside any incident window, `effective_cost` reconstructs exactly from `usage_amount × unit_price × (1 − credit_rate)` per SKU (catches silent drift between the cost model and the workload generators).

Each check is a script under `tests/data/`, run against every generated dataset before it's handed to Phase 2 — this is the "Data" tier of Section 16's testing pyramid, applied here rather than deferred to later phases.

## Chronological-integrity checks

- Train/validation/test windows (days 1–60 / 61–75 / 76–90) are non-overlapping and cover the full 90 days with no gaps.
- Every incident's `[injection_ts, injection_ts + duration_hours)` window falls entirely inside one split — none straddle a boundary.
- The held-out test split contains at least one `logging_regression` incident (the specific M2 requirement).

## M1 exit-gate checklist

The roadmap's one-line exit gate, expanded to what must literally be true before Phase 2 starts:

- [ ] 90 days × hourly grain generated for all five analytical tables plus `ground_truth_incidents`
- [ ] All data-quality gates above pass on the canonical `seed=42` dataset
- [ ] Reproducibility test passes (identical re-run; different output on a different seed)
- [ ] All causal sanity checks pass for all 10 injected incidents
- [ ] Chronological-integrity checks pass
- [ ] `manifest.json` published alongside the dataset with seeds, split boundaries, topology version, and incident list

## Contract with Phase 2

Phase 2's analytical spine is allowed to assume, without re-checking: the five tables conform to `phase0-schema-v1.md`; `ground_truth_incidents` exists and is never to be joined into model features; the chronological splits above are the only valid splits for backtesting; and every incident's true cause is exactly one of the four types in `phase0-incident-catalogue.md`, singly and without overlap. Anything Phase 2 needs beyond this list is a gap in this document, not something to assume silently.

## Sign-off

- [ ] Reproducibility test procedure agreed
- [ ] Causal sanity checks accepted as sufficient evidence the generators are internally consistent
- [ ] M1 exit-gate checklist accepted as the literal definition of "reproducible dataset with ground truth"
- [ ] Approved to proceed to Phase 2 (analytical spine)
