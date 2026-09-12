"""Tests for simulator/workloads/demand.py.

Split deliberately into two halves:

  * EXACT   — the deterministic factors. No tolerance, no seed sensitivity.
              These are the ones that catch a real regression the instant
              it lands.
  * STATISTICAL — properties that hold only in expectation, because the
              lognormal noise is real randomness. Fixed seed plus an
              explicit tolerance derived from the noise, not guessed.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from simulator.seeding import rng_for
from simulator.topology import build_topology
from simulator.workloads.demand import (
    SERVICE_DEMAND_PARAMS,
    STAGING_SCALE,
    _daily_shape,
    _growth,
    _unit_mean_lognormal,
    _weekly_factor,
    generate_demand,
    params_for,
)

RUN_START = pd.Timestamp("2026-01-01", tz="UTC")
RUN_DAYS = 90


@pytest.fixture(scope="module")
def hours() -> pd.DatetimeIndex:
    return pd.date_range(RUN_START, periods=24 * RUN_DAYS, freq="h", tz="UTC")


@pytest.fixture(scope="module")
def org():
    return build_topology()


def _demand(service, hours, seed: int = 42):
    """Generate with this service's own stream, the way cli.py should."""
    rng = rng_for(seed, f"workload:{service.project}:{service.name}")
    return generate_demand(service, hours, rng, run_start=hours[0])


# ---------------------------------------------------------------------------
# EXACT — deterministic factors
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("strength", [0.0, 0.10, 0.30, 0.35, 0.90, 1.0])
def test_daily_shape_averages_exactly_one(strength):
    """Convention (1): base_rate is only meaningful if this averages 1.0."""
    assert _daily_shape(np.arange(24.0), strength).mean() == pytest.approx(1.0, rel=1e-12)


def test_daily_shape_is_slice_invariant():
    """A partial-day slice must get the same multipliers the full day would."""
    full = _daily_shape(np.arange(24.0), 1.0)
    window = _daily_shape(np.array([9.0, 10.0, 11.0]), 1.0)
    assert np.allclose(window, full[9:12], rtol=1e-12, atol=0.0)


def test_daily_shape_is_flat_at_zero_strength():
    """Strength=0 means no daily variation."""
    assert np.allclose(_daily_shape(np.arange(24.0), 0.0), 1.0, rtol=1e-12, atol=0.0)


def test_daily_shape_peaks_in_the_workday():
    """Spec asks for a busy window over the working day, trough overnight."""
    curve = _daily_shape(np.arange(24.0), 1.0)
    assert 9 <= int(curve.argmax()) <= 17
    assert int(curve.argmin()) in range(0, 6)


def test_weekly_factor_is_exactly_one_on_weekdays(hours):
    params = SERVICE_DEMAND_PARAMS["web-frontend"]
    factor = _weekly_factor(hours, params)
    weekday = np.asarray(hours.dayofweek) < 5
    assert np.all(factor[weekday] == 1.0)
    assert np.all(factor[~weekday] == params.weekend_factor)


def test_growth_is_one_at_run_start(hours):
    params = SERVICE_DEMAND_PARAMS["checkout-api"]
    assert _growth(hours, params, hours[0])[0] == pytest.approx(1.0)


def test_growth_compounds_weekly(hours):
    params = SERVICE_DEMAND_PARAMS["checkout-api"]
    growth = _growth(hours, params, hours[0])
    assert growth[24 * 7] == pytest.approx(1.0 + params.growth_rate)
    assert growth[24 * 14] == pytest.approx((1.0 + params.growth_rate) ** 2)


def test_growth_is_anchored_to_run_start_not_the_slice(hours):
    """The days 76-90 regression: regenerating one window must not restart
    the trend at 1.0. This is the bug that made a standalone test-split
    generation come out ~12% low."""
    tail = hours[24 * 75 :]
    params = SERVICE_DEMAND_PARAMS["checkout-api"]
    assert np.allclose(
        _growth(hours, params, hours[0])[24 * 75 :],
        _growth(tail, params, hours[0]),
    )


def test_unit_mean_lognormal_is_exactly_one_when_sigma_is_zero():
    rng = rng_for(42, "workload")
    assert np.all(_unit_mean_lognormal(rng, 0.0, 100) == 1.0)


# ---------------------------------------------------------------------------
# EXACT — config resolution
# ---------------------------------------------------------------------------


def test_every_topology_service_has_demand_params(org):
    """Add a service to topology.py and forget its params here, and this
    fails now rather than halfway through a 90-day cli.py run."""
    for service in org.all_services():
        params_for(service)


def test_params_for_rejects_unknown_service(org):
    from dataclasses import replace as dc_replace

    ghost = dc_replace(org.get_service("checkout-api"), name="does-not-exist")
    with pytest.raises(KeyError):
        params_for(ghost)


def test_staging_is_scaled_down_from_prod(org):
    prod = params_for(org.get_service("checkout-api", project="prod"))
    staging = params_for(org.get_service("checkout-api", project="staging"))
    assert staging.base_rate == pytest.approx(prod.base_rate * STAGING_SCALE)
    assert staging.weekend_factor == prod.weekend_factor  # shape unchanged


# ---------------------------------------------------------------------------
# EXACT — frame contract and row invariants
# ---------------------------------------------------------------------------


def test_frame_schema(org, hours):
    df = _demand(org.get_service("checkout-api"), hours)
    assert list(df.columns) == ["requests", "active_customers", "transactions"]
    assert df.index.name == "hour"
    assert len(df) == len(hours)
    for column in df.columns:
        assert np.issubdtype(df[column].dtype, np.integer), column


def test_row_invariants_hold_for_every_service(org, hours):
    for service in org.all_services():
        df = _demand(service, hours)
        assert (df.requests >= 1).all(), service.name
        assert (df.active_customers >= 1).all(), service.name
        assert (df.transactions >= 0).all(), service.name
        assert (df.active_customers <= df.requests).all(), service.name
        assert (df.transactions <= df.requests).all(), service.name


def test_same_seed_reproduces_and_different_seed_does_not(org, hours):
    service = org.get_service("checkout-api")
    assert _demand(service, hours, seed=42).equals(_demand(service, hours, seed=42))
    assert not _demand(service, hours, seed=42).equals(_demand(service, hours, seed=7))


def test_services_have_independent_streams(org, hours):
    """Per-service rng: checkout-api's history must not depend on whether
    web-frontend was generated first."""
    service = org.get_service("checkout-api")
    alone = _demand(service, hours)
    _ = _demand(org.get_service("web-frontend"), hours)
    after = _demand(service, hours)
    assert alone.equals(after)


# ---------------------------------------------------------------------------
# STATISTICAL — fixed seed, tolerance derived from the noise
# ---------------------------------------------------------------------------
# Week 1 has 120 weekday hours and the daily curve averages exactly 1.0 over
# whole days, so the only scatter is the lognormal: SE ~= sigma/sqrt(120)
# ~= 0.7%. Growth also lifts the week-1 mean by about (1+g)^0.5. rel=0.03 is
# ~4 SE and absorbs that offset - tight enough to catch a real break, loose
# enough never to flake.


@pytest.mark.parametrize("service_name", sorted(SERVICE_DEMAND_PARAMS))
def test_base_rate_is_the_average_weekday_hour(org, hours, service_name):
    service = org.get_service(service_name)
    params = params_for(service)
    df = _demand(service, hours)
    week_one = df[df.index < hours[0] + pd.Timedelta(days=7)]
    weekday_mean = week_one[week_one.index.dayofweek < 5].requests.mean()
    assert weekday_mean == pytest.approx(params.base_rate, rel=0.03)


@pytest.mark.parametrize("service_name", sorted(SERVICE_DEMAND_PARAMS))
def test_weekend_dip_matches_the_configured_factor(org, hours, service_name):
    service = org.get_service(service_name)
    params = params_for(service)
    df = _demand(service, hours)
    weekday = df[df.index.dayofweek < 5].requests.mean()
    weekend = df[df.index.dayofweek >= 5].requests.mean()
    assert weekend / weekday == pytest.approx(params.weekend_factor, rel=0.03)


@pytest.mark.parametrize("service_name", sorted(SERVICE_DEMAND_PARAMS))
def test_growth_shows_up_across_the_run(org, hours, service_name):
    service = org.get_service(service_name)
    params = params_for(service)
    df = _demand(service, hours)
    first_week = df.requests[: 24 * 7].mean()
    last_week = df.requests[-24 * 7 :].mean()
    expected = (1.0 + params.growth_rate) ** ((RUN_DAYS - 7) / 7)
    assert last_week / first_week == pytest.approx(expected, rel=0.05)
