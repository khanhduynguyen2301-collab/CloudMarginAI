"""Translate one service's resource metrics into billing_hourly rows.

Implements docs/phase1/workload-cost-model.md, "Cost model":

    effective_cost(sku, h) = usage_amount(sku, h) x unit_price(sku) x (1 - credit_rate)

Three conventions this module fixes, numbered on from capacity.py's:

9.  Noise lands on USAGE, never on cost. workload-cost-model.md asks for
    multiplicative lognormal cost noise; validation-plan.md asks that
    effective_cost reconstruct EXACTLY from usage x price x (1 - credit_rate)
    outside incident windows. Both hold if the noise is applied to
    usage_amount and cost is then a pure function of it. That is also what real
    billing looks like: exact arithmetic on metered usage, with the usage being
    the noisy thing.
10. `credits` is a positive discount magnitude, not a negative adjustment —
    validation-plan.md's cost-signs gate requires usage_amount, effective_cost
    and credits all >= 0. effective_cost = list_cost - credits.
11. `ingested_at` is derived from the row's own `hour`, never from the wall
    clock. A datetime.now() anywhere in this pipeline breaks the byte-identical
    rerun that is Phase 1's exit gate.

Every row this simulator produces has source="estimated" and
is_reconciled=False: there is no real billing export to reconcile against yet.

Storage lives in one region (STORAGE_REGION) rather than being split — data
sits in a primary region; cross-region replication would be its own line item,
and no incident in scope needs it. Storage grows slowly from a base, anchored
on run_start (demand.py convention 4), so regenerating one window matches the
full run's rows.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np
import pandas as pd

from simulator.cost_model.pricing import CREDIT_RATE, SKU_PRICES
from simulator.noise import unit_mean_lognormal
from simulator.topology import ORGANIZATION_ID, ResourceKind, Service

SCHEMA_VERSION = "1"
CURRENCY = "USD"
SOURCE = "estimated"
STORAGE_REGION = "us-central1"

# staging mirrors prod's per-request intensities exactly; only its stored data
# is smaller, since a staging environment does not accumulate a prod history.
STAGING_STORAGE_SCALE = 0.05


@dataclass(frozen=True)
class BillingParams:
    """Per-service usage intensities — how much of each SKU one unit of work burns.

    Which SKUs a service emits follows from these: db.cpu-hour only when
    db_cpu_ms_per_request > 0; compute.gpu-hour instead of compute.vcpu-hour
    when the service is a BATCH_ACCELERATOR.
    """

    vcpu_per_instance: float
    egress_kb_per_request: float
    log_kb_per_request: float  # the logging-regression lever
    db_cpu_ms_per_request: float = 0.0  # the query-regression lever; > 0 only with a DB
    storage_base_gb: float = 50.0
    storage_growth_per_week: float = 0.01
    sigma_usage: float = 0.04  # applied to usage — convention 9


def usage_to_billing_rows(
    service: Service,
    demand: pd.DataFrame,
    metrics: pd.DataFrame,
    rng: np.random.Generator,
) -> pd.DataFrame:
    """Generate clean billing_hourly rows (long format) for one service.

    Args:
        service: the Service being billed.
        demand: output of workloads.demand.generate_demand for this service.
        metrics: output of workloads.capacity.generate_capacity_and_reliability
            for this service.
        rng: this component's Generator — get it via
            `simulator.seeding.rng_for(master_seed, "cost_noise")` (a
            different component than "workload", per
            simulator-architecture.md's seed hierarchy).

    Returns:
        Long-format DataFrame with one row per (sku, hour): columns sku,
        hour, usage_amount, usage_unit, credits, effective_cost. The
        caller (cli.py) fills in organization_id, project_id, service,
        region, currency, is_reconciled, source, schema_version,
        ingested_at to produce full BillingHourlyRow instances (see
        simulator/schema.py).
    """
    raise NotImplementedError("TODO: implement the usage -> cost translation above")
