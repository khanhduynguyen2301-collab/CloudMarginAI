"""Clean (pre-incident) demand generation — feeds application_activity_hourly.

Implements docs/phase1/workload-cost-model.md, "Demand generation":

    requests(h) = base_rate(service)
                × daily_curve(hour_of_day)      # smooth peak ~09:00-17:00 local, trough overnight
                × weekly_factor(day_of_week)    # weekend ~= 0.55-0.7x weekday, per service
                × (1 + growth_rate)^(day / 7)   # gentle week-over-week growth
                × lognormal_noise(sigma_demand)
    active_customers(h) ~= requests(h) / avg_requests_per_customer(service), own smaller noise
    transactions(h) = requests(h) * conversion_rate(service)

No holiday calendar in v1 (documented limitation, not an oversight).
growth_rate: 0.5%-1.5% per week, service-dependent.
"""
from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd

from simulator.topology import Service


def generate_demand(
    service: Service,
    hours: pd.DatetimeIndex,
    rng: np.random.Generator,
) -> pd.DataFrame:
    """Generate clean hourly demand for one service over `hours`.

    Args:
        service: the Service to generate demand for (base_rate,
            growth_rate, weekend_factor, etc. are per-service constants
            you choose — pick values consistent with the SKUs/topology
            this service will later bill against in cost_model/billing.py).
        hours: the full run's hourly timestamp index (UTC).
        rng: this component's Generator — get it via
            `simulator.seeding.rng_for(master_seed, "workload")`, never
            instantiate a Generator directly.

    Returns:
        DataFrame indexed by `hour` with columns: requests (int),
        active_customers (int), transactions (int). `revenue` and `plan`
        are NOT computed here — the caller fills revenue=None and a
        static plan value, per Phase 0's deferred finance outcome.
    """
    raise NotImplementedError("TODO: implement the demand formula above")
