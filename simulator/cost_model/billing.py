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


# Calibration (see docs/phase1/decisions.md):
#
# ingestion-worker  log_kb_per_request=50 -> ~60 GB/day of ingestion. A 3.5x
#                   logging regression (the weakest specified) adds ~$72/day at
#                   $0.50/GB, clearing the $50/day materiality bar with margin.
#                   35 KB clears it by only 4%; do not lower this.
# checkout-api      db_cpu_ms_per_request=3500 -> ~$84/day of db.cpu-hour. A
#                   1.8x query regression (the weakest specified) adds ~$67/day.
# ml-training-job   8 GPUs x 4h/day x $2.10 = ~$65/day baseline. An idle
#                   accelerator holds them on around the clock: +20h x 8 x $2.10
#                   = ~$325/day. Clears trivially.
# recommendation-api vcpu_per_instance=8: the autoscaling incident only adds
#                   instances during hours where demand would have run fewer
#                   than the raised floor, so its dollar impact is modest —
#                   ~$25/day at 3x, ~$111/day at 5x. At 3x it clears the
#                   materiality bar ONLY under the service-level reading of the
#                   5% rule. See the OPEN entry in decisions.md.
SERVICE_BILLING_PARAMS: dict[str, BillingParams] = {
    "web-frontend": BillingParams(
        vcpu_per_instance=2.0,
        egress_kb_per_request=45.0,  # serves HTML/JS/assets
        log_kb_per_request=2.0,
        storage_base_gb=40.0,
    ),
    "checkout-api": BillingParams(
        vcpu_per_instance=2.0,
        egress_kb_per_request=8.0,
        log_kb_per_request=3.0,
        db_cpu_ms_per_request=3500.0,  # the query-regression lever
        storage_base_gb=200.0,  # order history
        storage_growth_per_week=0.015,
    ),
    "recommendation-api": BillingParams(
        vcpu_per_instance=8.0,  # inference-heavy; see calibration note
        egress_kb_per_request=6.0,
        log_kb_per_request=2.5,
        storage_base_gb=120.0,
    ),
    "ingestion-worker": BillingParams(
        vcpu_per_instance=2.0,
        egress_kb_per_request=1.0,
        log_kb_per_request=50.0,  # the logging-regression lever — see note
        storage_base_gb=600.0,  # ingested data accumulates
        storage_growth_per_week=0.02,
    ),
    "billing-worker": BillingParams(
        vcpu_per_instance=1.0,
        egress_kb_per_request=2.0,
        log_kb_per_request=4.0,
        storage_base_gb=80.0,
    ),
    "ml-training-job": BillingParams(
        vcpu_per_instance=0.0,  # billed as GPU-hours, not vCPU-hours
        egress_kb_per_request=0.0,
        log_kb_per_request=0.0,  # no per-request traffic to log
        storage_base_gb=800.0,  # training corpus + checkpoints
        storage_growth_per_week=0.005,
    ),
}

# Fixed columns for every row this simulator emits.
_FIXED = {
    "organization_id": ORGANIZATION_ID,
    "currency": CURRENCY,
    "is_reconciled": False,
    "source": SOURCE,
    "schema_version": SCHEMA_VERSION,
}


def params_for(service: Service) -> BillingParams:
    """Resolve billing intensities for `service`, scaling stored data on staging."""
    try:
        base = SERVICE_BILLING_PARAMS[service.name]
    except KeyError as exc:
        raise KeyError(
            f"no billing parameters for service {service.name!r} — add an entry to "
            "SERVICE_BILLING_PARAMS when the topology gains a service"
        ) from exc
    if service.project == "prod":
        return base
    return replace(base, storage_base_gb=base.storage_base_gb * STAGING_STORAGE_SCALE)


def _storage_gb(
    hours: pd.DatetimeIndex, params: BillingParams, run_start: pd.Timestamp
) -> np.ndarray:
    """Stored GB per hour: slow compounding growth from a base, anchored on run_start."""
    elapsed_days = np.asarray((hours - run_start) / pd.Timedelta(days=1), dtype=float)
    return params.storage_base_gb * (1.0 + params.storage_growth_per_week) ** (elapsed_days / 7.0)


def _usage_by_sku(
    service: Service,
    params: BillingParams,
    region_metrics: pd.DataFrame,
    region: str,
    run_start: pd.Timestamp,
) -> dict[str, np.ndarray]:
    """Clean (pre-noise) usage per SKU for one region, as arrays aligned to hours."""
    hours = pd.DatetimeIndex(region_metrics["hour"])
    requests = region_metrics["request_count"].to_numpy(dtype=float)
    instances = region_metrics["instance_count"].to_numpy(dtype=float)
    usage: dict[str, np.ndarray] = {}

    if service.resource_kind is ResourceKind.BATCH_ACCELERATOR:
        usage["compute.gpu-hour"] = instances  # instance_count IS the GPU count
    else:
        usage["compute.vcpu-hour"] = instances * params.vcpu_per_instance

    if params.egress_kb_per_request > 0:
        usage["network.egress-gb"] = requests * params.egress_kb_per_request / 1e6
    if params.log_kb_per_request > 0:
        usage["logging.ingested-gb"] = requests * params.log_kb_per_request / 1e6
    if params.db_cpu_ms_per_request > 0:
        usage["db.cpu-hour"] = requests * params.db_cpu_ms_per_request / 3.6e6

    if region == STORAGE_REGION:
        usage["storage.standard-gb-month"] = _storage_gb(hours, params, run_start)

    return usage


def usage_to_billing_rows(
    service: Service,
    demand: pd.DataFrame,
    metrics: pd.DataFrame,
    rng: np.random.Generator,
    *,
    run_start: pd.Timestamp,
) -> pd.DataFrame:
    """Generate clean billing_hourly rows for one service, long format.

    Args:
        service: the Service being billed.
        demand: this service's frame from workloads.demand. Kept for
            symmetry with the other generators; every driver billing needs is
            already per-region in `metrics`, so nothing is read from it today.
        metrics: this service's frame from workloads.capacity — long format,
            one row per (resource_id, hour), carrying region, instance_count and
            request_count.
        rng: this service's own cost Generator — get it via
            `rng_for(master_seed, f"cost_noise:{service.project}:{service.name}")`.
        run_start: first timestamp of the WHOLE run, even when `metrics` covers
            a subset (demand.py convention 4). Anchors storage growth.

    Returns:
        One row per (region, sku, hour), with every billing_hourly column filled:
        organization_id, project_id, service, sku, region, hour, usage_amount,
        usage_unit, credits, effective_cost, currency, is_reconciled, source,
        schema_version, ingested_at. Regions and SKUs are iterated in sorted
        order so the random stream does not depend on dict ordering.

        effective_cost == usage_amount x unit_price x (1 - CREDIT_RATE) holds
        to floating-point precision on every row (convention 9).
    """
    del demand  # see Args — retained in the signature, unused
    params = params_for(service)
    frames: list[pd.DataFrame] = []

    for region in sorted(metrics["region"].unique()):
        region_metrics = metrics[metrics["region"] == region].sort_values("hour")
        hours = pd.DatetimeIndex(region_metrics["hour"])
        n = len(hours)
        usage_by_sku = _usage_by_sku(service, params, region_metrics, region, run_start)

        for sku in sorted(usage_by_sku):
            price = SKU_PRICES[sku]
            clean = usage_by_sku[sku]
            # Convention 9: noise on usage, cost stays exact.
            usage = clean * unit_mean_lognormal(rng, params.sigma_usage, n)
            list_cost = usage * price.unit_price_usd
            credits = list_cost * CREDIT_RATE
            effective = list_cost - credits

            frames.append(
                pd.DataFrame(
                    {
                        "organization_id": _FIXED["organization_id"],
                        "project_id": service.project,
                        "service": service.name,
                        "sku": sku,
                        "region": region,
                        "hour": hours,
                        "usage_amount": usage,
                        "usage_unit": price.usage_unit,
                        "credits": credits,
                        "effective_cost": effective,
                        "currency": _FIXED["currency"],
                        "is_reconciled": _FIXED["is_reconciled"],
                        "source": _FIXED["source"],
                        "schema_version": _FIXED["schema_version"],
                        "ingested_at": hours,  # convention 11
                    }
                )
            )

    return pd.concat(frames, ignore_index=True)
