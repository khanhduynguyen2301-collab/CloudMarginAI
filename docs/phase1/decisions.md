# CloudMargin AI — Phase 1 Implementation Decisions

A running log of decisions made *while building* Phase 1, as distinct from the
four specs in this directory, which describe what to build. Each entry records
what was decided, why, and what it constrains downstream — so "why is it like
this?" has one place to look.

Specs stay specs. This file is append-only: supersede an entry with a new dated
one rather than editing it, so the reasoning trail survives.

---

## 2026-09-07 — `ground_truth_incidents` is the single ground-truth table name

**Settled in:** `docs/phase1/incident-injection-spec.md`

An earlier draft of that spec introduced `incident_ground_truth` as a "new v1.1
addition", unaware that `docs/phase0/incident-catalogue.md` had already frozen
and signed off `ground_truth_incidents` for the same purpose. Two names for one
table.

Resolved in favour of the Phase 0 name, which was signed off first. Two fields
the injection engine needs — `duration_hours` and `chronological_split` — are
layered on additively.

The rejected draft also used a flat `magnitude_field`/`magnitude_value` pair,
which cannot hold query-regression's two ground-truth values
(`latency_delta_pct` **and** `cost_delta_pct`) at once. Phase 0's
`expected_magnitude` JSON field has no such limit and is retained. That draft had
also dropped `resolved_incident_id` and `simulator_seed`; both restored, the
former being what makes MTTD / MTTR-cause / Top-3 / MRR computable by join rather
than by eye.

---

## 2026-09-11 — `base_rate` means "expected requests in an average weekday hour"

**Settled in:** `simulator/workloads/demand.py`, convention (1)

The multiplicative factors are normalized so the anchor stays meaningful:
`_daily_curve` averages exactly 1.0 across the 24 integer hours, and
`_weekly_factor` is exactly 1.0 on weekdays (not renormalized across the week).

The daily curve gets this for free. It is a 24h fundamental plus a 12h harmonic,
and each completes a whole number of periods over 24 integer hours, so both sum
to exactly zero — the curve averages exactly 1.0 regardless of how the
amplitudes or the phase skew are retuned. A hand-tuned 24-value lookup table
would need renormalizing after every nudge.

**Constrains:** every downstream generator. If either normalization breaks,
`base_rate` silently stops meaning anything and every derived dollar figure in
`cost_model/billing.py` shifts by a constant with no visible cause. The
`_daily_shape` mean-1.0 assertions in `tests/test_demand.py` are what hold this.

---

## 2026-09-11 — UTC is the reference locale for org_demo

**Settled in:** `simulator/workloads/demand.py`, convention (2)

`application_activity_hourly` has no `region` column (its grain is
product-service-hour), so there is nothing to key a per-region time offset off.
The spec's "peak ~09:00-17:00 local" is therefore implemented as 09:00-17:00
**UTC**, with `WORKDAY_CENTRE_HOUR_UTC = 13.0`.

**Known consequence, accepted:** the curve peaks at ~11:00 UTC, which is 06:00 US
Central and 07:00 US Eastern, but 13:00 in `europe-west1`. The simulated company
therefore reads as European-hours despite two of its three regions being US.
Mechanically harmless — Phase 2 learns whatever pattern exists — but it is a
deliberate choice, not an oversight. A centre near 20.0 would anchor to US
Eastern afternoons if the portfolio narrative ever needs that.

---

## 2026-09-11 — Multiplicative noise is corrected to mean exactly 1.0

**Settled in:** `simulator/workloads/demand.py`, convention (3).
**Moved 2026-09-12** to `simulator/noise.py` — see that entry below.

`rng.lognormal(mean=0, sigma=s)` has expectation `exp(s^2/2)`, not 1. Used raw as
a multiplier it adds a systematic upward drift that is indistinguishable from
`growth_rate` in the output. All multiplicative noise is divided by
`exp(sigma^2/2)`.

At σ=0.08 the uncorrected bias is only +0.32%, but it compounds with the real
trend and would quietly corrupt any growth rate Phase 2 tries to recover.

**Constrains:** every generator that adds multiplicative noise. Call
`simulator.noise.unit_mean_lognormal`, never `rng.lognormal` directly.

---

## 2026-09-12 — Growth is anchored on an explicit `run_start`, never `hours[0]`

**Settled in:** `simulator/workloads/demand.py`, convention (4)

`_growth` originally measured elapsed time from `hours[0]`, so the compounding
trend restarted at 1.0 for any call not handed the full run index. Regenerating
the held-out test window (days 76-90) on its own produced demand ~12% below the
same rows from a full-run generation — 36,317 vs 41,273 mean hourly requests for
`checkout-api` — because growth resumed from 1.0 instead of continuing at 1.1363.

Each run was internally consistent, so no data-quality gate in
`validation-plan.md` would have caught it. Days 76-90 is exactly the window
Phase 2's backtest harness reserves and is the most likely to be regenerated
alone.

`run_start` is now a **required** keyword argument, sourced from the CLI's
`--start-date` (the same value `manifest.json` records). Required rather than
defaulting to `hours[0]`, because a default that silently restores the old
behaviour is the bug.

**Constrains:** the noise stream is still *not* slice-invariant — a shorter
`hours` draws fewer values and lands elsewhere in the stream. So the rule for
every generator is: **generate the full run, then slice.** `run_start` only makes
the deterministic component correct by construction. Regression test:
`test_growth_is_anchored_to_run_start_not_the_slice`.

---

## 2026-09-12 — RNG streams are per-service, not per-component

**Settled in:** `simulator/workloads/demand.py` docstring;
`tests/test_demand.py::test_services_have_independent_streams`

**Supersedes** the four component-level seeds described in
`simulator-architecture.md` under "Reproducibility strategy" (`seed_workload`,
`seed_cost_noise`, `seed_incidents`, `seed_changes`). Workload generation now
derives one stream per service:

    rng_for(master_seed, f"workload:{service.project}:{service.name}")

With a single shared `"workload"` stream, the order in which services are
generated determines every service's data — so adding a seventh service, or
reordering the loop in `cli.py`, silently changes the history of the six that
came before it. Per-service streams make each service independent of iteration
order.

**Constrains:** `cli.py`. A shared-stream implementation fails
`test_services_have_independent_streams`. `capacity.py` should follow the same
pattern with its own prefix (`capacity:{project}:{name}`) so a later change to
demand's noise cannot shift capacity's draws. The other three component seeds are
unchanged for now; revisit if the same ordering problem appears in them.

---

## 2026-09-12 — `base_rate` is bounded below by the materiality threshold

**Settled in:** `simulator/workloads/demand.py`, calibration note above
`SERVICE_DEMAND_PARAMS`

Demand volumes are not free parameters. They set the dollar figures, which must
be large enough for injected incidents to be detectable at the governance bar in
`docs/phase0/threat-model-governance.md`: unexplained cost > $50/day, **or** > 5%
of expected cost, sustained ≥ 3 consecutive hours.

Worked for the weakest specified logging regression (3.5x, from
`incidents/types.py`) against `logging.ingested-gb` at $0.50/GB:

    extra $/day = baseline_GB_per_day x (3.5 - 1) x $0.50
    clears $50/day  =>  baseline > 40 GB/day

At `ingestion-worker`'s `base_rate=50_000` the realized volume is ~1.19M
requests/day (1,188,916, measured on a generated run), putting break-even at
~35 KB/request — which clears the bar by only 4% ($52/day), too thin to build a
detector against once noise is layered on.

**Constrains:** `cost_model/billing.py` should use **~50 KB/request** for
`ingestion-worker`: ~59 GB/day baseline, ~$74/day from a 3.5x regression. Do not
retune `ingestion-worker`'s `base_rate` without redoing this arithmetic.

---

## 2026-09-12 — Shared noise helper lives in `simulator/noise.py`

**Settled in:** `simulator/noise.py`

The mean-1.0 correction above is a project-wide invariant, not a demand-model
detail. `billing.py`, `changes/` and `incidents/engine.py` all sample
multiplicatively and need the same helper, and whoever writes them will look for
a module named `noise.py` — not inside `demand.py` for a leading-underscore name.

`demand.py` keeps `_unit_mean_lognormal` as a thin delegation under the same
name, so its call sites and `tests/test_demand.py` are unaffected. Verified the
delegation returns byte-identical arrays for the same seed.

**Constrains:** new generators import `from simulator.noise import
unit_mean_lognormal`. Do not reach into another module's privates for it.

---

## 2026-09-12 — `pytest.ini` bounds test collection to the project

**Settled in:** `pytest.ini`

`pythonpath = "."` and `testpaths = "tests"`.

With no pytest config file anywhere in the project, pytest found no rootdir
anchor, escaped upward, and failed collecting `D:\WpSystem` — a Windows-managed
folder whose ACLs `os.stat` cannot read (`OSError: [WinError 1337]`). `testpaths`
bounds collection to the project. `pythonpath` puts the repo root on `sys.path`,
which is separately necessary: without it, bare `pytest` cannot import the
`simulator` package (only `python -m pytest` worked, because `-m` adds the cwd).

**Constrains:** nothing in the generators — recorded here because the failure
mode is obscure enough to waste an hour twice.

---

## OPEN — is "5% of expected cost" service-level or org-level?

**Raised by:** the calibration entry above. **Owner:** Phase 2.

`docs/phase0/threat-model-governance.md` sets materiality as unexplained cost
> $50/day **or** > 5% of expected cost. It does not say whether "expected cost"
is scoped to the affected service or to the whole organization. The two readings
differ by roughly an order of magnitude, and the 5% route may well trigger before
the dollar route under either.

Until this is resolved, the calibration above deliberately clears the **dollar**
route, which is unambiguous.

**Action:** resolve before Phase 2's detector implements the anomaly policy, and
add a gate to `simulator/validate.py` asserting every injected incident actually
clears the bar it is supposed to be detectable at. Nothing currently verifies
this.

---

## OPEN — how does a service's demand fan out across regions?

**Raised by:** starting `workloads/capacity.py`. **Owner:** this phase.

`demand.py` produces one series per service with no region, because
`application_activity_hourly` has no `region` column. But
`resource_metrics_hourly` is grained at resource-hour and carries both
`resource_id` and `region`, and `topology.py` gives every service all three
regions. Nothing in the specs says how one service's hourly requests split across
regions, or how many `resource_id`s a service-region has.

Candidates: fixed per-service region weights with one resource per service-region
(simple; three parallel signals); or a variable resource pool where
`instance_count` is the pool size (more realistic, more bookkeeping, and
`resource_id` churns over time).

**Action:** decide in `capacity.py` and append the outcome here. `resource_id`
naming must be deterministic from the topology rather than the rng — Phase 3 will
name a `resource_id` as an incident's cause, and that string has to be
reproducible. It must also include `project`: prod and staging both contain
`web-frontend` and `checkout-api`, so a name without it collides across projects
and would break the uniqueness gate in `validate.py`.
