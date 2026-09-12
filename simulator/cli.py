"""CLI entry point: `simulate --seed 42 --start-date 2026-01-01 --days 90 --out ./output`

Per docs/phase1/simulator-architecture.md, "Repository layout":
`simulator/cli.py` — `simulate --seed --start-date --days --config` entry point.

The argument parser below is fully implemented (it's plumbing). `main`'s
orchestration body is the one TODO that ties every other module together —
leave it for last, once topology/workloads/cost_model/changes/incidents/
labels/validate all have working implementations to call into.
"""
from __future__ import annotations

import argparse
import sys


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="simulate",
        description="Generate a reproducible CloudMargin AI Phase 1 synthetic dataset.",
    )
    parser.add_argument("--seed", type=int, required=True, help="master seed for the whole run")
    parser.add_argument("--start-date", type=str, default="2026-01-01", help="YYYY-MM-DD, UTC")
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument(
        "--config", type=str, default=None, help="optional path to override defaults"
    )
    parser.add_argument("--out", type=str, default="./output", help="output directory for this run")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Orchestration TODO (docs/phase1/simulator-architecture.md +
    docs/phase1/validation-plan.md tie together here):

      1. topology.build_topology()
      2. For each service: workloads.demand.generate_demand ->
         workloads.capacity.generate_capacity_and_reliability
      3. cost_model.billing.usage_to_billing_rows per service
      4. changes.deployments.generate_deployments +
         changes.resource_changes.generate_resource_changes (background) per service
      5. incidents.engine.place_incidents(...) then
         incidents.engine.inject(...) for each placed incident
      6. labels.ground_truth.write_ground_truth(...)
      7. Assemble final tables, write manifest.json (master seed, derived
         seeds, date range, split boundaries, topology version, cost-model
         version, injected incident IDs — see simulator-architecture.md,
         "Run manifest")
      8. validate.run_all_gates(...) — refuse to write a "done" marker if
         any gate fails
    """
    parser = build_arg_parser()
    _args = parser.parse_args(argv)  # named _args until the orchestration below uses it
    raise NotImplementedError("TODO: wire the pipeline above together")


if __name__ == "__main__":
    sys.exit(main())
