"""Tests for intraday cost helpers and pair exits."""

from __future__ import annotations

from src.config import get_persona_config
from src.intraday.costs import passes_min_net_edge, round_trip_costs
from src.intraday.pair_exits import pair_exit_reason, pair_leg_quantities


def test_round_trip_costs_positive():
    config = get_persona_config("revert")
    costs = round_trip_costs(config, 1000.0, 1005.0, 50)
    assert costs > 0


def test_min_net_edge_rejects_tiny_moves():
    config = get_persona_config("flux")
    assert passes_min_net_edge(config, 1000.0, 1000.5, 50) is False


def test_pair_exit_reason():
    assert pair_exit_reason(0.1, 0.2, 2.5) == "pair_convergence"
    assert pair_exit_reason(3.0, 0.2, 2.5) == "pair_stop"
    assert pair_exit_reason(1.0, 0.2, 2.5) is None


def test_pair_leg_quantities():
    config = get_persona_config("spread")
    qty_a, qty_b = pair_leg_quantities(config, 400_000, 100.0, 99.5, 200.0, 1.0)
    assert qty_a >= 1
    assert qty_b >= 1
