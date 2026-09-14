"""Tests for simulator/noise.py.

noise.py is shared infrastructure: demand, capacity and billing all depend on
it, and the mean-1.0 correction it implements is a project-wide invariant
(docs/phase1/decisions.md). These tests exist so the helper is covered
directly rather than only through the generators that call it.
"""

from __future__ import annotations

import numpy as np
import pytest

from simulator.noise import unit_mean_lognormal
from simulator.seeding import rng_for
from simulator.workloads.demand import _unit_mean_lognormal

# ---------------------------------------------------------------------------
# EXACT
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sigma", [0.0, -0.1])
def test_non_positive_sigma_returns_exact_ones(sigma):
    """A generator can disable its noise by setting sigma to 0 without branching."""
    out = unit_mean_lognormal(rng_for(42, "x"), sigma, 50)
    assert out.shape == (50,)
    assert np.all(out == 1.0)


def test_non_positive_sigma_does_not_consume_the_stream():
    """Documented behaviour, not an accident: sigma=0 returns before drawing, so
    it leaves the Generator untouched. Toggling a service's sigma between 0 and
    >0 therefore shifts every later draw in that stream — which is exactly why
    each generator gets its own stream (decisions.md)."""
    a = rng_for(42, "x")
    b = rng_for(42, "x")
    unit_mean_lognormal(a, 0.0, 1000)
    assert a.random() == b.random()


def test_output_shape_matches_size():
    assert unit_mean_lognormal(rng_for(42, "x"), 0.08, 2160).shape == (2160,)


def test_output_is_strictly_positive():
    """Multiplicative noise must never zero out or flip a value."""
    out = unit_mean_lognormal(rng_for(42, "x"), 0.5, 100_000)
    assert (out > 0).all()


def test_same_seed_reproduces_and_different_seed_does_not():
    a = unit_mean_lognormal(rng_for(42, "x"), 0.08, 1000)
    b = unit_mean_lognormal(rng_for(42, "x"), 0.08, 1000)
    c = unit_mean_lognormal(rng_for(7, "x"), 0.08, 1000)
    assert np.array_equal(a, b)
    assert not np.array_equal(a, c)


def test_demand_delegation_is_byte_identical():
    """demand._unit_mean_lognormal is a thin wrapper kept for its call sites and
    tests. If someone re-implements it locally, this catches the drift."""
    a = _unit_mean_lognormal(rng_for(42, "x"), 0.08, 1000)
    b = unit_mean_lognormal(rng_for(42, "x"), 0.08, 1000)
    assert np.array_equal(a, b)


# ---------------------------------------------------------------------------
# STATISTICAL — fixed seed, tolerance derived from the noise
# ---------------------------------------------------------------------------
# For lognormal(0, s) the std is ~s for small s, so over n draws the standard
# error of the mean is ~s/sqrt(n). At s=0.08, n=200_000 that is ~1.8e-4; the
# abs=1e-3 tolerance is ~5 SE.


def test_mean_is_one():
    out = unit_mean_lognormal(rng_for(42, "x"), 0.08, 200_000)
    assert out.mean() == pytest.approx(1.0, abs=1e-3)


def test_correction_removes_the_lognormal_bias():
    """The reason this function exists. rng.lognormal(mean=0, sigma=s) has
    expectation exp(s^2/2), not 1 — at s=0.5 that is a +13.3% bias, which
    used raw as a multiplier would masquerade as growth. The corrected helper
    must sit at 1.0 while the raw draw sits at exp(s^2/2)."""
    sigma, n = 0.5, 200_000
    raw = rng_for(42, "x").lognormal(mean=0.0, sigma=sigma, size=n)
    corrected = unit_mean_lognormal(rng_for(42, "x"), sigma, n)

    expected_bias = float(np.exp(sigma**2 / 2.0))  # ~1.1331
    assert raw.mean() == pytest.approx(expected_bias, rel=0.01)
    assert corrected.mean() == pytest.approx(1.0, rel=0.01)
    assert corrected.mean() < raw.mean()


def test_spread_scales_with_sigma():
    """Larger sigma, wider noise — sanity that sigma is actually wired through."""
    narrow = unit_mean_lognormal(rng_for(42, "x"), 0.02, 50_000)
    wide = unit_mean_lognormal(rng_for(42, "x"), 0.20, 50_000)
    assert wide.std() > 5 * narrow.std()
