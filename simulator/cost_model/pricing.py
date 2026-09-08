"""Fixed, documented per-SKU unit price table.

Transcribed exactly from docs/phase1/workload-cost-model.md, "Cost model".
Illustrative GCP-like rates — never present these as real GCP pricing
(say so in the repo README too, per that doc's sign-off checklist).

Fully implemented — this is a declarative table, not a design decision
left for the generators.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SkuPrice:
    sku: str
    usage_unit: str
    unit_price_usd: float


# credit_rate: small flat sustained-use-style discount applied uniformly
# to every SKU so it doesn't create a spurious per-SKU pattern.
CREDIT_RATE = 0.03

SKU_PRICES: dict[str, SkuPrice] = {
    "compute.vcpu-hour": SkuPrice("compute.vcpu-hour", "vCPU-hour", 0.031),
    "compute.gpu-hour": SkuPrice("compute.gpu-hour", "GPU-hour", 2.10),
    "storage.standard-gb-month": SkuPrice("storage.standard-gb-month", "GB-month (hourly fraction)", 0.020 / 730),
    "network.egress-gb": SkuPrice("network.egress-gb", "GB", 0.085),
    "logging.ingested-gb": SkuPrice("logging.ingested-gb", "GB", 0.50),
    "db.cpu-hour": SkuPrice("db.cpu-hour", "vCPU-hour", 0.096),
}
