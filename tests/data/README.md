# Causal sanity checks

Per `docs/phase1/validation-plan.md`, "Causal sanity checks": each check
listed there ("For every `logging_regression` ground-truth row...", etc.)
should be a script in this directory, run against every generated dataset
before it's handed to Phase 2. This is the "Data" tier of the blueprint's
testing pyramid (Section 16), applied here rather than deferred.

These are separate from `simulator/validate.py`'s gate functions, which
`simulator/cli.py` runs automatically as part of every `simulate` call —
the scripts here are meant to be run explicitly against a finished dataset
(e.g. in CI, or by hand before trusting a run), and can be slower / more
thorough than the inline gates.

Nothing here yet — add one test file per incident type once
`simulator/incidents/engine.py` is implemented.
