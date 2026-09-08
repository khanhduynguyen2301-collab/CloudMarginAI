"""CloudMargin AI — Phase 1 data simulator.

Generates a reproducible 90-day synthetic dataset (workload, billing,
change events, and labeled incidents) for `org_demo`, per:
  - docs/phase0/schema-v1.md          (the 5 analytical tables this writes)
  - docs/phase0/incident-catalogue.md (the ground_truth_incidents table)
  - docs/phase1/simulator-architecture.md
  - docs/phase1/workload-cost-model.md
  - docs/phase1/incident-injection-spec.md
  - docs/phase1/validation-plan.md

Module map (see simulator-architecture.md, "Repository layout"):
  topology.py     — fixed org/project/service/region graph (implemented)
  schema.py       — row dataclasses matching the frozen table schemas (implemented)
  seeding.py      — deterministic sub-seed derivation (implemented)
  workloads/      — demand + capacity/reliability generators (TODO)
  cost_model/     — unit pricing + usage -> billing_hourly (TODO)
  changes/        — background deployment/resource-change generators (TODO)
  incidents/      — injection engine + placement (TODO)
  labels/         — ground_truth_incidents writer (TODO)
  validate.py     — data-quality gates + causal sanity checks (TODO)
  cli.py          — `simulate --seed ...` entry point (TODO)
"""
