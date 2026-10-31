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

---

## 2026-09-13 — RESOLVED: demand fans out by fixed region weights, one resource per service-region

**Settled in:** `simulator/workloads/capacity.py`, conventions (5)–(8).
**Resolves** the OPEN entry "how does a service's demand fan out across
regions?" above.

Chosen: the simple candidate. Each service's hourly requests are split by fixed
weights — `us-central1` 0.50, `us-east1` 0.30, `europe-west1` 0.20 — with exactly
one resource per service-region. The variable resource pool was rejected: none of
the four incident types needs it, and churning `resource_id`s would make Phase 3's
"name the resource that caused this" harder to score.

- `resource_id = f"{project}-{service}-{region}-{ordinal:03d}"`, derived from the
  topology and never the rng, and including `project` so prod and staging cannot
  collide.
- Regions are iterated in `sorted()` order, so reordering the weights dict
  cannot shift the random stream.
- `ml-training-job` (BATCH_ACCELERATOR) runs in one region on a fixed schedule:
  8 GPUs, 02:00–06:00 UTC, off otherwise. It does not follow demand.
- **staging runs in one region** with replica bounds scaled by 0.10 (floors 1
  and 2). Found while calibrating: staging carries 4% of prod's demand, and
  splitting that three ways left `staging/checkout-api` in `europe-west1` below
  one instance's worth of traffic for the whole run — pinned at `min_replicas`,
  perfectly flat cost, and an undetectable autoscaling incident.

**Constrains:** per-hour `request_count` summed over a service's resources
matches demand within ±(number of regions), from rounding each region
separately. `tests/test_capacity.py` enforces that every demand-driven service's
`instance_count` actually varies.

---

## 2026-09-13 — Noise lands on usage, never on cost

**Settled in:** `simulator/cost_model/billing.py`, conventions (9)–(11)

Two specs conflicted. `workload-cost-model.md` asks for multiplicative lognormal
cost noise; `validation-plan.md` asks that `effective_cost` reconstruct
**exactly** from `usage_amount × unit_price × (1 − credit_rate)`. Both hold if
the noise is applied to `usage_amount` and cost is then pure arithmetic on it —
which is also how real billing works: metering is noisy, the arithmetic is not.
Verified to 3.55e-15 across all 138,240 billing rows of a run.

Two further rules from the same module:

- `credits` is a **positive** discount magnitude (`effective_cost = list_cost −
  credits`), because the cost-signs gate requires all three columns ≥ 0.
- `ingested_at` is derived from the row's own `hour`, never the wall clock. One
  `datetime.now()` anywhere breaks the byte-identical rerun.

**Constrains:** every module that writes billing rows. `incidents/engine.py`
inherits this as its convention (16).

---

## 2026-09-14 — Reserved causal vocabulary; deployments are prod-only

**Settled in:** `simulator/changes/deployments.py`, conventions (12)–(13)

- **(12)** `logging-config` and `query-layer` (in `deployments.changed_component`)
  and `schedule_removed` and `update_scaling_policy` (in
  `resource_changes.change_type`) are written **only** by
  `incidents/engine.py`, to mark the change that caused an incident. If
  background noise emitted them, Phase 3's ranker could find a true cause by
  string match instead of by evidence, and its Top-3 accuracy would mean nothing.
- **(13)** The frozen `deployments` schema has no `project_id`, and prod and
  staging both contain `web-frontend` and `checkout-api`. A staging deploy would
  be an ambiguous candidate cause for a prod incident, so deployments are
  generated for prod only. Staging's change activity lives in `resource_changes`,
  which does carry `project_id`.

Expected volume per 90-day run, from the configured rates: ~74 background
deployments (prod, one per 6–9 days per service) and ~81 background config
changes (one per resource per 20 days, 18 resources) — roughly 7 benign deploys
per injected incident, so "a change happened nearby" is not by itself evidence.
Checked over 400 seeds: realized counts track these expectations within 2.5%.

**Known schema gap, accepted:** `resource_changes` has no id column, so
`(resource_id, changed_at)` is its de facto primary key — the only handle Phase 3
has to cite a config change. `validate.py`'s uniqueness gate enforces it.

---

## 2026-09-16 — The injection engine is pure, ratio-based, and measures its own ground truth

**Settled in:** `simulator/incidents/engine.py`, conventions (14)–(17)

- **(14) Pure, and expressed as ratios.** `inject` never modifies its inputs;
  it returns new frames. Inside the window it rescales each clean value by the
  ratio the parameter change implies, rather than regenerating the window. That
  keeps every hour's own noise draw, so the parameter is the only thing that
  changes — regenerating would put a noise discontinuity at the window edge that
  a detector could find for the wrong reason. The rewrite is exact because every
  affected quantity is linear in its parameter. **One exception:** idle
  accelerator off-hours have a clean GPU count of 0, where a ratio is undefined;
  those hours get a fresh draw from the incidents stream.
- **(15) Ground-truth magnitude is measured, not copied.** Only
  `log_byte_multiplier` is the input parameter. `latency_delta_pct`,
  `cost_delta_pct`, `idle_cost_rate` and `instance_count_delta` are outcomes,
  measured by differencing injected against clean. The sampled input is
  recoverable only by replaying `simulator_seed`.
- **(16) Cost is recomputed from usage**, never scaled alongside it, so the
  exact-reconstruction rule above holds inside incident windows too.
- **(17) No incident touches demand.** `application_activity_hourly` is a clean
  control series for the whole run — which denies Phase 3 a business-demand
  shortcut.

Also fixed here:

- **Placement is a fixed allocation**, not sampled: train gets logging ×2,
  query ×2, idle, autoscaling; validation gets query and idle; test gets
  logging and autoscaling. 6/2/2 and "exactly one logging regression in test"
  therefore hold for every seed. Starts are rejection-sampled, with ≥ 48 h
  between incidents on the same service. Placement succeeded on 500/500 seeds.
- **The causal event lands 1–3 hours before its incident**, never at the same
  instant, so the true cause cannot be found with an equality join on time.
- **Causal `deployment_id`s are provisional**, offset by 5000 so they cannot
  collide with background ids (`dep-<service>-5001`, …). `cli.py` **must**
  renumber all deployments chronologically when it merges the two streams, or
  the offset becomes a tell of its own.
- **An autoscaling cause is attached to the primary-region resource**, because
  `resource_changes` is resource-grained but the policy change is service-wide.
  Phase 3 must score an autoscaling cause at service level, not by exact
  `resource_id` match.

---

## 2026-09-16 — FINDING: `autoscaling_error` often falls below the materiality bar

**Found by:** `tests/test_engine.py`, recorded as `xfail(strict=True)` with the
diagnosis in its reason string. **Feeds:** the OPEN materiality entry above.
**Status:** fix not yet chosen.

Across 25 seeds (50 autoscaling injections), **24 fell below $50/day** and 12
below 5% of org-level expected cost (~$680/day, so a ~$34/day bar). The weakest
was $17.5/day at a 3.27× floor. The other three types cleared both bars on every
seed: logging regression ≥ $72.5/day, query regression ≥ $54.9/day, idle
accelerator ≥ $316.8/day.

The magnitude range is not the cause. `min_replicas = 4` is one global floor,
while regions carry 50/30/20 of traffic. `us-central1`'s trough is ~7 instances,
so a 3× floor of 12 lifts only **7.5% of its hours, by 0.17 instances on
average** — and that region is half the spend. `europe-west1` is lifted 71% of
the time, but it is 20% of the spend.

Options:

1. **Region-aware replica floor** in `capacity.py`, so each region's floor sits
   near its own trough — what a real autoscaling floor is. Most principled;
   shifts the clean baseline, so `tests/test_capacity.py` needs re-checking.
2. Raise `recommendation-api`'s `vcpu_per_instance` (8 → ~14). Cheapest, but it
   tunes a price to hit a threshold.
3. Accept it, and stop counting sub-threshold incidents as detector misses in
   Phase 2/3 scoring.

Recommended: option 1.

**Updates the OPEN materiality entry:** its proposed `validate.py` gate was not
added. Materiality is currently enforced by `tests/test_engine.py` across 25
seeds, not by the per-run gates.

---

## 2026-09-24 — Ground truth lives in its own subdirectory, as JSON Lines, and never in the manifest

**Settled in:** `simulator/labels/ground_truth.py`, conventions (18)–(20)

- **(18)** Written to `output_dir/ground_truth/ground_truth_incidents.jsonl`,
  never beside the analytical tables. A detector that globs the run directory
  structurally cannot reach it — a comment saying "don't read this" protects
  nothing.
- **(19)** JSON Lines, not parquet (overriding the stub's suggestion).
  `expected_magnitude` has different keys per incident type, which a columnar
  schema could only hold as a union struct full of nulls or an opaque JSON
  string. It is ~10 rows, and an auditor can read it without a parquet reader.
- **(20)** `manifest_entries()` builds the manifest's incident list **without**
  magnitudes, per `simulator-architecture.md`. `cli.py` must use it rather than
  building the list inline, so the rule lives next to the data it protects.

Byte-identical across directories and across write → read → write. Written with
`newline="\n"` so Windows produces the same bytes as CI.

`_TIMESTAMP_FIELDS` is derived from the dataclass's type hints. That derivation
must accept both `Optional[datetime]` and `datetime | None`: they are different
union origins before Python 3.14, and checking only one silently skips the
other; a probe test covers every spelling.

---

## 2026-09-24 — Line endings are LF, enforced by `.gitattributes`

**Settled in:** `.gitattributes` (`* text=auto eol=lf`, `*.parquet binary`)

Five test files pasted in on Windows had CRLF endings while the rest were LF.
Source line endings do not change the simulator's output, but the mix grows
with every paste and makes diffs noisy. The rule now lives in the repo rather
than in each machine's personal git config, so every checkout gets the same
bytes.

---

## 2026-09-24 — The validator re-derives everything from the output tables

**Settled in:** `simulator/validate.py`, conventions (21)–(24)

- **(21)** Every check reads only the assembled tables — no clean frames, no
  generator internals. A causal check that asked `engine.py` what it did would
  only prove `engine.py` agrees with itself.
- **(22)** Each incident is compared with the same window shifted by ±1–4
  **whole weeks** — the nearest shift inside the run and clear of every incident
  on that service. Whole weeks cancel the daily and weekly cycles exactly;
  dividing by the traffic ratio cancels growth. Tolerances were measured over
  20 seeds (200 incidents) with ≥ 2× headroom, then checked: 60/60 seeds pass
  every gate.
- **(23)** `tables` is the contract for `cli.py`: the five analytical tables,
  each with exactly its `schema.py` fields. Anything else — above all a
  ground-truth frame — fails the schema gate.
- **(24)** `run_all_gates` returns a `ValidationReport`, falsy on any failure,
  with `summary()` for the CLI to print. A gate that raises is recorded as that
  gate failing, so one broken table still yields a complete report.

Two gates were added beyond `validation-plan.md`: **referential integrity**
(every `resource_id` in `resource_changes` has metrics — an orphan makes Phase
3's join silently empty) and **manifest vs ground truth**.

**Constrains `cli.py`:**

- `application_activity_hourly` is **prod-only**. Its grain is
  product-service-hour with no `project_id`, so staging rows would duplicate
  prod's keys — and staging has no real customers anyway.
- `manifest.json` must carry `master_seed`, `derived_seeds`, `date_range`,
  `split_boundaries`, `topology_version`, `cost_model_version` and `incidents`.

---

## 2026-09-24 — Spec errata found while writing `validate.py`

**Settled in:** `simulator/validate.py`. **Action:** correct the specs.

1. **Idle-accelerator billing.** `validation-plan.md` says `compute.gpu-hour`
   usage stays "unchanged from baseline"; `incident-injection-spec.md` says it
   "continues at its normal active rate" around the clock, which is what the
   engine does — so total usage *rises*. Implemented the injection spec: every
   window hour bills the full 8-GPU pool. Fix `validation-plan.md`.
2. **Nullable fields.** The plan lists only `revenue` and `remediated_at`.
   `gpu_utilization` must be null for services with no GPU (0.0 there would read
   as an idle accelerator), and latency is null for the batch job. The gate
   enforces both directions.
3. **"Below 5%" means at or below.** The engine clips GPU utilization at exactly
   0.05 and that value occurs, so the check is inclusive. Fix the wording — or
   change the clip, not the check.
4. **Two new gates** (referential integrity, manifest vs ground truth) belong in
   the plan's gate list.

---

## 2026-10-31 — The two change generators need separate random streams

**Found while:** writing these entries. **Constrains:** `cli.py`, and the
docstrings of `changes/deployments.py` and `changes/resource_changes.py`.
**Action:** change both docstrings before `cli.py` is written.

Both docstrings currently say to seed with
`rng_for(master_seed, f"changes:{project}:{name}")` — the same key. Called with
two fresh Generators from that key, the two generators start from identical
random numbers. Measured over 200 seeds of prod services:

| Seeding | Config changes landing on a deploy hour | corr(# deploys, # changes) |
|---|---|---|
| Same key, two fresh Generators | 1.51% | **+0.62** |
| Same Generator instance, used in turn | 0.48% | +0.14 |
| Separate keys | 0.53% | +0.09 |

The first row is the bug: a service with many deploys would also get many
config changes, and the two would coincide three times as often — a spurious
"change cluster" pattern that a Phase 3 ranker could learn. The residual +0.09
in the last row is structural (services with more regions and faster release
cadences have more of both), not stream sharing.

Decided: separate keys, `deployments:{project}:{name}` and
`resource_changes:{project}:{name}`. Sharing one Generator instance would also
avoid most of the correlation, but it makes `resource_changes` depend on how many
numbers `deployments` happened to draw — the iteration-order coupling the
per-service-stream entry above exists to prevent.

**Also settled for `cli.py`** — the full stream naming: `workload:`,
`capacity:`, `cost_noise:`, `deployments:` and `resource_changes:`, each suffixed
`{project}:{name}`, plus one `incidents` stream threaded through
`place_incidents` and then every `inject` call, in placement order.
