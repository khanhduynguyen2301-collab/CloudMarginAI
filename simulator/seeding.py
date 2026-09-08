"""Deterministic seed derivation for the CloudMargin AI simulator.

One master seed must reproduce an entire run byte-for-byte — this is the
literal reproducibility test in docs/phase1/validation-plan.md ("Run
`simulate --seed 42` twice ... pass requires byte-identical hashes").

Every generator module must call `rng_for` to get its own independent
random stream instead of sharing one global RNG or instantiating
`np.random.default_rng()` directly — per
docs/phase1/simulator-architecture.md, "Reproducibility strategy": "no
shared global RNG state, so adding a module later can't silently shift
another module's random stream."

This file is fully implemented — it's plumbing, not a design decision.
"""
from __future__ import annotations

import hashlib

import numpy as np

# Every module that draws randomness needs an entry here. Add a new
# component name when a new generator needs its own independent stream —
# never reuse an existing component's seed for a second purpose.
COMPONENTS = ("workload", "cost_noise", "incidents", "changes")


def derive_seed(master_seed: int, component: str) -> int:
    """Deterministically derive a 32-bit sub-seed for `component` from `master_seed`.

    Same (master_seed, component) always yields the same sub-seed, on any
    machine, any Python version, any run order. `component` should be one
    of COMPONENTS (not enforced here, so a typo doesn't crash — but a typo
    silently creates an untracked new stream, so keep COMPONENTS in sync).
    """
    digest = hashlib.sha256(f"{master_seed}:{component}".encode("utf-8")).digest()
    return int.from_bytes(digest[:4], byteorder="big")


def rng_for(master_seed: int, component: str) -> np.random.Generator:
    """Return a fresh, independent numpy Generator for `component`.

    Usage:
        demand_rng = rng_for(master_seed, "workload")
        cost_rng = rng_for(master_seed, "cost_noise")
    """
    return np.random.default_rng(derive_seed(master_seed, component))
