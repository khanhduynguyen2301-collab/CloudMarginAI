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

Four conventions this module fixes, because everything downstream inherits
them:

1. `base_rate` means "expected requests in an average WEEKDAY hour, on day 0".
   `_daily_curve` is normalized so its 24 values average exactly 1.0, and
   `_weekly_factor` is exactly 1.0 on weekdays. Without that normalization,
   base_rate silently stops meaning anything and every derived dollar figure in
   cost_model/billing.py is off by a constant nobody remembers.
2. Timestamps are UTC and UTC *is* the reference locale for org_demo — there is
   no region column on application_activity_hourly to key a per-region offset
   off, so "peak ~09:00-17:00 local" is implemented as peak 09:00-17:00 UTC.
   Phase 2 derives hour-of-day features from this same column and will see the
   busy window where WORKDAY_CENTRE_HOUR_UTC puts it.
3. The lognormal noise is divided by exp(sigma^2 / 2) so it has mean exactly
   1.0. rng.lognormal(mean=0, sigma=s) has expectation exp(s^2/2), which would
   otherwise add a small systematic upward drift on top of growth_rate.
4. `run_start` is passed explicitly rather than taken from `hours[0]`, so the
   growth trend is identical whether you generate the full 90 days or a single
   window. The noise stream is *not* slice-invariant — a shorter `hours` draws
   fewer values — so the rule remains: generate the full run, then slice.
   `run_start` makes the deterministic half correct by construction; this
   convention covers the rest.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np
import pandas as pd

from simulator.noise import unit_mean_lognormal
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
    """Per-service demand constants.

    These live here rather than on `topology.Service` deliberately: topology.py
    is a transcription of a frozen doc decision, and hanging tunables off it
    would quietly turn it into a config file. Calibrating these numbers is a
    workload-model concern, so it belongs to the workload model.
    """

    base_rate: float  # expected requests in an average weekday hour, day 0
    weekend_factor: float  # Sat/Sun multiplier; doc range 0.55-0.7 for user-facing
    growth_rate: float  # per week, doc range 0.005-0.015
    diurnal_strength: float  # 0.0 = flat, 1.0 = full user-facing swing
    avg_requests_per_customer: float
    conversion_rate: float
    sigma_demand: float = 0.08
    sigma_customers: float = 0.04


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


def params_for(service: Service) -> DemandParams:
    """Resolve the demand constants for `service`, applying the staging scale."""
    try:
        base = SERVICE_DEMAND_PARAMS[service.name]
    except KeyError as exc:
        raise KeyError(
            f"no demand parameters for service {service.name!r} — add an entry to "
            "SERVICE_DEMAND_PARAMS when the topology gains a service"
        ) from exc
    if service.project == "prod":
        return base
    return replace(base, base_rate=base.base_rate * STAGING_SCALE)


def _daily_shape(hour_of_day: np.ndarray, diurnal_strength: float) -> np.ndarray:
    """The daily multiplier for raw hour-of-day values, averaging exactly 1.0.

    Normalization is computed against all 24 integer hours rather than against
    the values passed in, so a partial-day slice gets the same multipliers the
    full series would — convention (1) in the module docstring.
    """

    def raw(h: np.ndarray) -> np.ndarray:
        theta = 2.0 * np.pi * (h - WORKDAY_CENTRE_HOUR_UTC) / 24.0
        theta_skewed = 2.0 * np.pi * (h - WORKDAY_CENTRE_HOUR_UTC - _HARMONIC_SKEW_HOURS) / 24.0
        return (
            1.0
            + diurnal_strength * _DAILY_AMP_FUNDAMENTAL * np.cos(theta)
            + diurnal_strength * _DAILY_AMP_HARMONIC * np.cos(2.0 * theta_skewed)
        )

    return raw(np.asarray(hour_of_day, dtype=float)) / raw(np.arange(24.0)).mean()


def _daily_curve(hours: pd.DatetimeIndex, diurnal_strength: float) -> np.ndarray:
    """Hour-of-day multiplier for each timestamp in `hours`.

    `diurnal_strength` scales the swing: 1.0 is a full user-facing workday
    profile, 0.0 is flat. Normalization happens after scaling, so the mean-1.0
    guarantee holds at every strength.
    """
    return _daily_shape(np.asarray(hours.hour, dtype=float), diurnal_strength)


def _weekly_factor(hours: pd.DatetimeIndex, params: DemandParams) -> np.ndarray:
    """Day-of-week multiplier: exactly 1.0 Mon-Fri, `weekend_factor` Sat/Sun.

    Weekdays are pinned at 1.0 rather than renormalized across the whole week,
    so `base_rate` reads as "average weekday hour" — convention (1).
    """
    dow = np.asarray(hours.dayofweek)  # Monday=0 ... Sunday=6
    return np.where(dow >= 5, params.weekend_factor, 1.0)


def _growth(hours: pd.DatetimeIndex, params: DemandParams, run_start: pd.Timestamp) -> np.ndarray:
    """Week-over-week compounding trend, in fractional days from the run start.

    Fractional rather than whole days so the trend is smooth instead of stepping
    at midnight — a midnight step is exactly the kind of artefact a change-point
    detector in Phase 2 would happily latch onto.

    Anchored on the explicit `run_start`, never on `hours[0]`: a call given only
    part of the run (the held-out test window, say) must continue the trend, not
    restart it at 1.0. See convention (4).
    """
    if len(hours) == 0:
        return np.zeros(0)
    elapsed_days = np.asarray((hours - run_start) / pd.Timedelta(days=1), dtype=float)
    return (1.0 + params.growth_rate) ** (elapsed_days / 7.0)


def _unit_mean_lognormal(rng: np.random.Generator, sigma: float, size: int) -> np.ndarray:
    """Multiplicative lognormal noise with mean exactly 1.0 — convention (3).

    Thin delegation to simulator.noise, which is where the shared helper lives
    now that billing.py, changes/ and incidents/ all need it. Kept under this
    name so existing call sites and tests are unaffected.
    """
    return unit_mean_lognormal(rng, sigma, size)


def generate_demand(
    service: Service,
    hours: pd.DatetimeIndex,
    rng: np.random.Generator,
    run_start: pd.Timestamp,
) -> pd.DataFrame:
    """Generate clean hourly demand for one service over `hours`.

    Args:
        service: the Service to generate demand for (base_rate,
            growth_rate, weekend_factor, etc. are per-service constants
            you choose — pick values consistent with the SKUs/topology
            this service will later bill against in cost_model/billing.py).
        hours: the full run's hourly timestamp index (UTC).
        rng: this service's own Generator — get it via
            `rng_for(master_seed, f"workload:{service.project}:{service.name}")`.
            Per-service rather than one shared "workload" stream, so adding or
            reordering services does not shift every other service's history.
            Never instantiate a Generator directly.
        run_start: first timestamp of the WHOLE run, even when `hours` is a
            subset of it. Sourced from the CLI's --start-date, the same value
            manifest.json records. See convention (4).

    Returns:
        DataFrame indexed by `hour` with columns: requests (int),
        active_customers (int), transactions (int). `revenue` and `plan`
        are NOT computed here — the caller fills revenue=None and a
        static plan value, per Phase 0's deferred finance outcome.
    """
    params = params_for(service)
    n = len(hours)

    expected = (
        params.base_rate
        * _daily_curve(hours, params.diurnal_strength)
        * _weekly_factor(hours, params)
        * _growth(hours, params, run_start)
    )
    requests = expected * _unit_mean_lognormal(rng, params.sigma_demand, n)

    # Floor of 1: an overnight trough x weekend factor x a low noise draw can
    # round to zero for a small service, and Phase 2 divides by requests to get
    # cost-per-request. A zero there is a blown-up feature, not a data point.
    requests_int = np.maximum(np.rint(requests).astype(np.int64), 1)

    customers = (requests / params.avg_requests_per_customer) * _unit_mean_lognormal(
        rng, params.sigma_customers, n
    )
    customers_int = np.clip(np.rint(customers).astype(np.int64), 1, requests_int)

    transactions_int = np.clip(
        np.rint(requests * params.conversion_rate).astype(np.int64), 0, requests_int
    )

    return pd.DataFrame(
        {
            "requests": requests_int,
            "active_customers": customers_int,
            "transactions": transactions_int,
        },
        index=pd.Index(hours, name="hour"),
    )
