"""Shared noise helpers for the simulator's generators.

Any generator adding multiplicative noise uses `unit_mean_lognormal` rather
than calling `rng.lognormal` directly. This is not a style preference:
`rng.lognormal(mean=0, sigma=s)` has expectation `exp(s^2/2)`, not 1, so used
raw as a multiplier it adds a small systematic upward drift that is
indistinguishable from a real growth trend in the output — and would quietly
corrupt any growth rate Phase 2 tries to recover.

See docs/phase1/decisions.md, "Lognormal noise is corrected to mean exactly
1.0".
"""
from __future__ import annotations

import numpy as np


def unit_mean_lognormal(
    rng: np.random.Generator, sigma: float, size: int
) -> np.ndarray:
    """Multiplicative lognormal noise with mean exactly 1.0.

    Args:
        rng: the caller's own Generator — see decisions.md on per-service
            streams; never instantiate one here.
        sigma: lognormal sigma. Zero or negative returns exact ones, so a
            generator can disable its noise without branching.
        size: number of draws.

    Returns:
        Array of `size` multipliers centred on exactly 1.0.
    """
    if sigma <= 0.0:
        return np.ones(size)
    return rng.lognormal(mean=0.0, sigma=sigma, size=size) / np.exp(sigma**2 / 2.0)
