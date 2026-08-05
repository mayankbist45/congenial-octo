"""Tests for intraday growth/value style classification."""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest

from src.config import get_persona_config
from src.intraday.agent import IntradayAgent
from src.intraday.fundamental_style import StyleProfile, classify_snapshot
from src.intraday.models import IntradaySignal, RankedStock
from src.models import FundamentalSnapshot


@pytest.fixture
def calan_config():
    return get_persona_config("calan")


def test_classify_growth_momentum(calan_config):
    snap = FundamentalSnapshot(
        symbol="NVDA",
        pe_ratio=45.0,
        peg_ratio=1.2,
        earnings_growth_pct=25.0,
    )
    profile = classify_snapshot(snap, calan_config, "NVDA")
    assert profile.label == "growth"
    assert profile.intraday_bias == "momentum"


def test_classify_value_mean_reversion(calan_config):
    snap = FundamentalSnapshot(
        symbol="HDFCBANK",
        pe_ratio=14.0,
        earnings_growth_pct=5.0,
    )
    profile = classify_snapshot(snap, calan_config, "HDFCBANK")
    assert profile.label == "value"
    assert profile.intraday_bias == "mean_reversion"


def test_classify_value_trap(calan_config):
    snap = FundamentalSnapshot(
        symbol="TRAP",
        pe_ratio=50.0,
        earnings_growth_pct=-12.0,
    )
    profile = classify_snapshot(snap, calan_config, "TRAP")
    assert profile.label == "value_trap"
    assert profile.intraday_bias == "mean_reversion"


def test_gate_vetoes_momentum_on_value(calan_config, monkeypatch):
    from src.intraday import agent as agent_mod

    agent = IntradayAgent(calan_config)
    signal = IntradaySignal(
        "HDFCBANK",
        "opening_range_breakout",
        "BUY",
        datetime(2026, 8, 5, 10, 0),
        100.0,
        98.0,
        104.0,
        "test",
    )
    ranked = RankedStock(
        symbol="HDFCBANK",
        score=80.0,
        turnover=2e8,
        volume_expansion=2.0,
        range_pct=1.0,
        gap_pct=1.0,
        vwap_distance_pct=0.1,
        prior_level_distance_pct=0.5,
        trend_cleanliness=0.6,
        in_play=True,
    )
    idx = pd.date_range("2026-08-05 09:45", periods=6, freq="5min")
    frame = pd.DataFrame(
        {
            "open": [99.0] * len(idx),
            "high": [100.5] * len(idx),
            "low": [98.5] * len(idx),
            "close": [100.0] * len(idx),
            "volume": [200_000] * len(idx),
        },
        index=idx,
    )
    style = StyleProfile(
        "HDFCBANK",
        "value",
        "mean_reversion",
        14.0,
        None,
        5.0,
        None,
        "low P/E",
    )

    monkeypatch.setattr(agent_mod, "get_style_profile", lambda _cfg, _sym: style)

    gate = agent._calan_gate_rules(
        signal,
        ranked,
        frame,
        datetime(2026, 8, 5, 10, 0),
        0.0,
        {"symbol": "HDFCBANK", "confidence": 0.7},
        50.0,
    )
    assert gate["decision"] == "veto"
    assert "revert persona" in gate["rationale"]
