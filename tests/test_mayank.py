"""Tests for Mayank hybrid intraday persona."""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest

from src.config import get_persona_config
from src.intraday.agent import IntradayAgent
from src.intraday.fundamental_style import StyleProfile
from src.intraday.mayank import (
    collect_mayank_signals,
    is_mayank_persona,
    mayank_catalyst_rules,
    profile_to_dict,
    style_params_snapshot,
)
from src.intraday.models import RankedStock


@pytest.fixture
def mayank_config():
    return get_persona_config("mayank")


def test_mayank_persona_loads(mayank_config):
    assert mayank_config.persona_id == "mayank"
    assert mayank_config.market_id == "india_equity"
    assert is_mayank_persona(mayank_config)
    strategies = mayank_config.get("intraday.strategies", [])
    assert "scalping" in strategies
    assert "vwap_fade" in strategies
    assert mayank_config.path("universe.file").name == "nifty500.csv"


def test_style_params_snapshot(mayank_config):
    params = style_params_snapshot(mayank_config)
    assert params["value_pe_threshold"] == 18
    assert params["high_pe_threshold"] == 35
    assert "opening_range_breakout" in params["momentum_strategies"]


def test_mayank_catalyst_rules_routes_by_style(mayank_config, monkeypatch):
    from src.intraday import mayank as mayank_mod

    growth = StyleProfile(
        "RELIANCE", "growth", "momentum", 28.0, 1.5, 15.0, None, "growth"
    )
    value = StyleProfile(
        "HDFCBANK", "value", "mean_reversion", 14.0, None, 5.0, None, "value"
    )

    def _profile(_cfg, symbol):
        if symbol.upper() == "RELIANCE":
            return growth
        if symbol.upper() == "HDFCBANK":
            return value
        return None

    monkeypatch.setattr(mayank_mod, "get_style_profile", _profile)

    ranked = [
        RankedStock(
            "RELIANCE", 90, 2e8, 2.5, 1.2, 1.0, 0.2, 0.5, 0.7, in_play=True, reasons=["gap"]
        ),
        RankedStock(
            "HDFCBANK", 85, 2e8, 1.5, 0.8, 0.2, 2.5, 0.3, 0.3, in_play=False, reasons=["fade"]
        ),
    ]
    result = mayank_catalyst_rules(mayank_config, ranked)
    symbols = {w["symbol"] for w in result["watchlist"]}
    assert "RELIANCE" in symbols
    assert "HDFCBANK" in symbols
    by_sym = {w["symbol"]: w for w in result["watchlist"]}
    assert by_sym["RELIANCE"]["playbook"] == "momentum"
    assert by_sym["HDFCBANK"]["playbook"] == "mean_reversion"


def test_collect_mayank_signals_filters_strategies(mayank_config, monkeypatch):
    from src.intraday import mayank as mayank_mod

    growth = StyleProfile(
        "RELIANCE", "growth", "momentum", 28.0, 1.5, 15.0, None, "growth"
    )

    monkeypatch.setattr(mayank_mod, "get_style_profile", lambda _c, _s: growth)

    idx = pd.date_range("2026-08-06 09:45", periods=8, freq="5min")
    frame = pd.DataFrame(
        {
            "open": [100.0] * len(idx),
            "high": [101.0] * len(idx),
            "low": [99.0] * len(idx),
            "close": [100.5] * len(idx),
            "volume": [250_000] * len(idx),
        },
        index=idx,
    )
    ranked = [
        RankedStock(
            "RELIANCE", 90, 2e8, 2.5, 1.2, 1.0, 0.2, 0.5, 0.7, in_play=True
        )
    ]
    signals = collect_mayank_signals(
        mayank_config, {"RELIANCE": frame}, ranked, {}
    )
    for _, signal, _ in signals:
        assert signal.strategy in mayank_mod.MOMENTUM_STRATEGIES


def test_mayank_gate_routes_revert_strategies(mayank_config):
    agent = IntradayAgent(mayank_config)
    from src.intraday.models import IntradaySignal

    signal = IntradaySignal(
        "HDFCBANK",
        "vwap_fade",
        "SELL",
        datetime(2026, 8, 6, 11, 0),
        100.0,
        101.0,
        98.0,
        "fade test",
    )
    ranked = RankedStock(
        "HDFCBANK", 80, 2e8, 1.5, 0.8, 0.1, 2.0, 0.2, 0.3, in_play=True
    )
    idx = pd.date_range("2026-08-06 09:45", periods=6, freq="5min")
    frame = pd.DataFrame(
        {
            "open": [100.0] * len(idx),
            "high": [101.0] * len(idx),
            "low": [99.0] * len(idx),
            "close": [100.0] * len(idx),
            "volume": [200_000] * len(idx),
        },
        index=idx,
    )
    gate = agent._gate_rules(
        signal, ranked, frame, datetime(2026, 8, 6, 11, 0), 0.0, None, 20.0
    )
    assert "decision" in gate


def test_profile_to_dict_includes_pe():
    profile = StyleProfile(
        "TCS", "growth", "momentum", 30.0, 1.8, 12.0, 10.0, "test"
    )
    row = profile_to_dict(profile)
    assert row["fundamental_style"] == "growth"
    assert row["pe_ratio"] == 30.0
    assert "scalping" in row["preferred_strategies"]
