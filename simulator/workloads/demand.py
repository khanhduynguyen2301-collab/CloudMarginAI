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

from dataclasses import dataclass
from datetime import datetime

import numpy as np
import pandas as pd

from simulator.topology import Service


# Phase centre of the daily curve's fundamental, UTC. NOT the argmax: the
# harmonic skew below pulls the actual maximum earlier, to ~11:00, leaving a
# plateau that spans roughly 09:00-17:00 as the spec asks. See convention (2).
WORKDAY_CENTRE_HOUR_UTC = 13.0

# Fundamental (24h) and second-harmonic (12h) amplitudes of the daily curve at
# full strength. The second harmonic is negative, which broadens the workday
# into a plateau and deepens the overnight trough instead of leaving a pure
# sinusoid's symmetric hump; HARMONIC_SKEW_HOURS phase-shifts it so the morning
# ramp is steeper than the evening decay, which is how real traffic behaves.
_DAILY_AMP_FUNDAMENTAL = 0.45
_DAILY_AMP_HARMONIC = -0.15
_HARMONIC_SKEW_HOURS = 1.5

@dataclass(frozen=True)
class DemandParams:
    """Per-service constants for demand generation.

    These are the knobs you turn to make a service's demand look realistic
    and consistent with the SKUs/topology it will later bill against in
    cost_model/billing.py. The values here are just examples; you can change
    them to whatever you like, as long as they are plausible.
    """

    base_rate: float  # requests per hour at hour 0 (UTC) of day 0
    growth_rate: float  # week-over-week growth rate (0.005-0.015)
    weekend_factor: float  # weekend demand relative to weekday (0.55-0.95)
    avg_requests_per_customer: float  # used to derive active_customers
    conversion_rate: float  # used to derive transactions
    diurnal_strength: float = 1.0  # scales the daily curve's swing about 1.0;
    # 1.0 is the full user-facing shape, 0.0 is flat. Queue-driven and batch
    # services sit low: a backlog drains overnight instead of going idle.
    sigma_demand: float = 0.03  # lognormal noise sigma on hourly requests


# Calibration note for cost_model/billing.py — do not tune ingestion-worker's
# base_rate without re-checking this. The governance doc's materiality bar is
# unexplained cost > $50/day (or > 5% of expected cost). A logging regression at
# its *weakest* specified magnitude (3.5x, incidents/types.py) against
# logging.ingested-gb at $0.50/GB clears the dollar route only if:
#
#     baseline_GB_per_day x (3.5 - 1) x $0.50 > $50   =>   > 40 GB/day
#
# At 50_000 req/weekday-hour, ingestion-worker actually realizes ~1.19M
# requests/day, which puts the break-even at ~35 KB/request — but 35 KB clears
# the bar by only 4% ($52/day), too thin to build a detector against once noise
# is layered on. Use ~50 KB/request in billing.py: ~60 GB/day baseline, and a
# 3.5x regression adds ~$75/day. The 5% route may well clear first — but the doc
# does not say whether "5% of expected cost" is service-level or org-level, and
# that ambiguity changes the answer by an order of magnitude. Open question for
# Phase 2; worth a gate in validate.py asserting every injected incident really
# does clear the bar it is supposed to be detectable at.
SERVICE_DEMAND_PARAMS: dict[str, DemandParams] = {
    # Request-serving, user-facing: full diurnal swing, real weekend dip.
    "web-frontend": DemandParams(
        base_rate=120_000,
        weekend_factor=0.60,
        growth_rate=0.010,
        diurnal_strength=1.00,
        avg_requests_per_customer=45.0,
        conversion_rate=0.020,
    ),
    "checkout-api": DemandParams(
        base_rate=40_000,
        weekend_factor=0.62,
        growth_rate=0.012,
        diurnal_strength=1.00,
        avg_requests_per_customer=12.0,
        conversion_rate=0.150,
    ),
    "recommendation-api": DemandParams(
        base_rate=60_000,
        weekend_factor=0.70,
        growth_rate=0.015,
        diurnal_strength=0.90,
        avg_requests_per_customer=25.0,
        conversion_rate=0.010,
    ),
    # Queue-driven workers: flatter, since a queue drains overnight rather than
    # going idle, and less weekend-sensitive.
    "ingestion-worker": DemandParams(
        base_rate=50_000,
        weekend_factor=0.80,
        growth_rate=0.008,
        diurnal_strength=0.35,
        avg_requests_per_customer=400.0,
        conversion_rate=0.0,
    ),
    "billing-worker": DemandParams(
        base_rate=8_000,
        weekend_factor=0.85,
        growth_rate=0.006,
        diurnal_strength=0.30,
        avg_requests_per_customer=250.0,
        conversion_rate=0.900,
    ),
    # Batch accelerator: demand is near-meaningless here — capacity.py runs this
    # service off a fixed job schedule, not off requests. Kept almost flat so it
    # cannot masquerade as a diurnal signal.
    "ml-training-job": DemandParams(
        base_rate=500,
        weekend_factor=0.95,
        growth_rate=0.005,
        diurnal_strength=0.10,
        avg_requests_per_customer=50.0,
        conversion_rate=0.0,
        sigma_demand=0.05,
    ),
}

# staging mirrors prod's shape at a fraction of the volume — it exists for
# noise/contrast, per simulator-architecture.md, not to carry incidents.
STAGING_SCALE = 0.04


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
