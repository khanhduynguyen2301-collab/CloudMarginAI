"""Translate one service's demand + resource metrics into billing_hourly rows.

Implements docs/phase1/workload-cost-model.md, "Cost model":

    effective_cost(sku, h) = usage_amount(sku, h) * unit_price(sku) * (1 - credit_rate)

with small multiplicative lognormal noise (sigma ~= 0.03-0.05) layered on
top of the deterministic usage->cost mapping — see that doc for why the
noise magnitude matters (has to be big enough that prediction intervals
mean something, small enough that a 3-hour incident stays cleanly
separable from noise).

A service emits one row per (sku, hour) it's billed against — e.g.
checkout-api emits compute.vcpu-hour AND db.cpu-hour rows every hour;
ml-training-job emits compute.gpu-hour only. Which SKUs a given service
uses is a decision this module owns (informed by the service's
resource_kind and the incident it's the home of, per
topology.INCIDENT_SERVICE_MAP — e.g. ingestion-worker needs a
logging.ingested-gb row since it's the logging-regression incident's
home).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from simulator.topology import Service


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
