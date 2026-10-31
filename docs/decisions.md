# CloudMargin AI — Project Direction Decisions

Decisions that change the direction of the whole project, as distinct from
`docs/phase1/decisions.md`, which records how Phase 1 was built. When an entry
here overrides the project plan (v1.2) or a signed-off spec, it says so, and the
plan stays as written — this file is where the change is recorded.

Append-only: supersede an entry with a new dated one rather than editing it.

---

## 2026-10-31 — CloudMargin is a long-term engineering project, built the way real teams build

**Supersedes:** the earlier intent to run CloudMargin in production for real
users.

CloudMargin is a long-term project built for its own sake, and is not expected
to have outside users. It is a job-shadowing exercise: every part should be
designed, tested, documented and reviewed the way a professional team would do
it. The scope is deliberately bigger and longer-term than the original
case-study plan it grew from; plan v1.2 remains the baseline, extended rather
than replaced.

**Constrains:** "done" means engineered properly, not merely working. There is
no uptime target, but there are the habits real teams keep: tests for every
module, recorded decisions, reproducible builds, reviewed changes, CI.

---

## 2026-10-31 — Engineering rigor at fintech level

CloudMargin is treated as a financial system: it computes costs, quantifies
savings, and gates actions behind approval. Concrete rules that follow:

- **Money is never floating point where it is recorded or acted on.** PostgreSQL
  stores money as `NUMERIC` (as `incidents` and `recommendations` already do),
  and the backend uses an exact decimal type (`BigDecimal` in Java).
  `billing_hourly`'s `FLOAT64` stays — it is an analytical fact table, and
  per-row noise is already far larger than float error.
- **Consequential actions are idempotent and audited.** The `idempotency_key`
  on `recommendations` and the `outbox_events` table are load-bearing, not
  optional.
- **Results are reproducible.** Every dataset comes from a seed and a manifest,
  and every evaluation number names the dataset and seed it came from.

---

## 2026-10-31 — No hosted LLM API; the model is built from an open base model

**Supersedes:** plan v1.2's "Gemini via Vertex AI as investigation
orchestrator".

No hosted LLM API is used anywhere in the project. The investigation agent is
built from an open base model on Hugging Face — fine-tuned and served locally —
using a mainstream agent framework.

**Scope of the change:** Phases 2–3 are unaffected. Expected cost, anomaly
detection and root-cause ranking are statistical and classic ML, and never
needed an LLM. Only the investigation agent (Phase 4) changes.

**Constrains:**

- Model size is bounded by available GPU memory. Choose the base model after
  measuring what fits, with parameter-efficient fine-tuning (LoRA/QLoRA) as the
  default.
- The agent's safety stance from plan v1.2 is unchanged: read-only by default,
  no arbitrary shell or SQL, and human approval before any consequential action.
  A self-hosted model needs these guardrails more, not less.
- Fine-tuning needs training data. Investigation examples will come from the
  simulator's incidents, so the answer-key separation in Phase 1 extends to
  them: no fine-tuning example may be built from the test split.

---

## 2026-10-31 — Real data alongside simulated data

The project uses real data, not only the simulator.

- **Real source:** billing export to BigQuery from a small Google Cloud project
  of the author's own, giving real daily and weekly patterns, real noise and
  real quirks such as late corrections.
- **The simulator's role shifts:** from *the* dataset to the test harness that
  carries an answer key. Real data has no labels; planting incidents into it
  with `incidents/engine.py` gives real background behaviour and a known answer
  at once.
- **External checks:** the Numenta Anomaly Benchmark (real series with labelled
  anomalies) tests whether the detector generalizes beyond the simulator; the
  FinOps Foundation's FOCUS sample data checks billing shape.

**Constrains:**

- Every reported number says which data it came from — simulated with injected
  incidents, a real export with injected incidents, or NAB — because each proves
  something different.
- `connectors/` moves earlier: it is needed as soon as Phase 2 has a detector to
  run on real data.
- Planting into real data works today for the logging and query regressions,
  which only rescale table values. The idle-accelerator and autoscaling
  injections read simulator capacity settings (`gpu_count`, `min_replicas`) and
  need adapting first.

---

## 2026-10-31 — Four parts: frontend, backend, database, authentication

CloudMargin has four explicit parts: frontend, backend, database and
authentication.

**Authentication is new as a requirement.** Plan v1.2 had four seeded demo
accounts with roles enforced in the API. Real authentication adds sign-up and
sign-in, password hashing, sessions or tokens, and protection on every
endpoint. The existing `user_roles` table is the foundation, and roles remain
enforced server-side, never only hidden in the UI.

---

## 2026-10-31 — Languages: one clear job each

**Extends:** plan v1.2's stack (Next.js frontend, FastAPI backend, Python
analytics).

| Part | Language | Why |
|---|---|---|
| `simulator/`, `analytics/`, model training | Python | NumPy, pandas, scikit-learn and Hugging Face are Python |
| `frontend/` | TypeScript (Next.js) | JavaScript with types; types are generated from the shared API contract |
| `backend/` — incidents, approvals, recommendations, auth | Java (Spring Boot) | Standard in finance; Spring Security for auth; strong transaction support for approvals and the outbox |
| `connectors/`, `workers/` — billing pulls, Pub/Sub consumers, outbox publisher | Go | The language of cloud infrastructure tooling; built for concurrent jobs; small binaries for Cloud Run |
| One bounded, correctness-critical component — likely the remediation policy engine | Rust (maybe) | Strongest guarantees where a wrong answer is unacceptable |

**Not adopted:** C#, which overlaps Java's role (choose one or the other, not
both), and plain JavaScript, which TypeScript supersedes. No Node.js backend:
Java and Go already cover that ground.

**Constrains:**

- **A language boundary is a service boundary with a contract.** Tables and
  messages are defined once, language-neutrally (Protocol Buffers or JSON
  Schema), and each language's types are generated from that.
  `simulator/schema.py` stops being the source of truth and becomes generated or
  checked against it.
- **Each language brings its own toolchain:** build, tests, linter, pinned
  dependencies, container image and CI job. Add a language only when the phase
  that needs it starts.
- **The simulator stays Python.** Its byte-identical reruns depend on NumPy's
  random streams; a port could not reproduce them.
- **Keep the service count small.** Plan v1.2's "modular distributed system over
  microservice sprawl" still holds; a language is never the reason to split a
  service.
- **Order:** finish Phase 1 in Python; Go first, for the billing-export
  connector; Java when backend and auth work starts; Rust last, if at all.
