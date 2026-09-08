"""Working example test — seeding.py is fully implemented, so this passes
today. Use it as the pattern for tests you write against your own
implementations (test_topology.py is the second worked example).
"""
from simulator.seeding import derive_seed, rng_for


def test_derive_seed_is_deterministic():
    assert derive_seed(42, "workload") == derive_seed(42, "workload")


def test_derive_seed_differs_by_component():
    assert derive_seed(42, "workload") != derive_seed(42, "incidents")


def test_derive_seed_differs_by_master_seed():
    assert derive_seed(42, "workload") != derive_seed(7, "workload")


def test_rng_for_is_reproducible():
    rng1 = rng_for(42, "cost_noise")
    rng2 = rng_for(42, "cost_noise")
    assert rng1.random() == rng2.random()
