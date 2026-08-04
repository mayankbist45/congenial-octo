"""Tests for calan SIP-ORB-VWAP intraday module."""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest
from unittest.mock import MagicMock

from src.config import get_persona_config
from src.intraday.agent import IntradayAgent
from src.intraday.models import IntradaySignal, RankedStock
from src.intraday.ranking import IntradayRanker
from src.intraday.strategies import load_strategies, ScalpingStrategy
from src.intraday.simulator import IntradaySimulator


def _sample_frame(gap_pct: float = 1.2, volume: int = 200_000) -> pd.DataFrame:
    base = 100.0
    open_price = base * (1 + gap_pct / 100.0)
    idx = pd.date_range("2026-08-03 09:15", periods=12, freq="5min")
    closes = [open_price]
    for i in range(1, len(idx)):
        closes.append(closes[-1] + (0.3 if i < 8 else 0.8))
    data = {
        "open": closes,
        "high": [c + 0.2 for c in closes],
        "low": [c - 0.2 for c in closes],
        "close": closes,
        "volume": [volume] * len(idx),
    }
    return pd.DataFrame(data, index=idx)


@pytest.fixture
def calan_config():
    return get_persona_config("calan")


def test_load_strategies_excludes_scalping_by_default(calan_config):
    names = [s.name for s in load_strategies(calan_config)]
    assert names == ["opening_range_breakout", "vwap_pullback"]
    assert ScalpingStrategy().signals("TEST", _sample_frame(), calan_config) == []


def test_ranker_marks_stocks_in_play(calan_config):
    ranker = IntradayRanker(calan_config)
    frame = _sample_frame(gap_pct=1.5, volume=250_000)
    ranked = ranker.rank({"RELIANCE": frame}, {"RELIANCE": {"close": 100.0, "high": 101, "low": 99}})
    assert len(ranked) == 1
    assert ranked[0].in_play is True


def test_agent_rules_catalyst_scan(calan_config):
    agent = IntradayAgent(calan_config)
    frame = _sample_frame()
    ranked = [
        RankedStock(
            symbol="RELIANCE",
            score=80.0,
            turnover=2e8,
            volume_expansion=2.5,
            range_pct=1.2,
            gap_pct=1.5,
            vwap_distance_pct=0.2,
            prior_level_distance_pct=0.5,
            trend_cleanliness=0.7,
            in_play=True,
            reasons=["test"],
        )
    ]
    result = agent.catalyst_scan(ranked, {"RELIANCE": frame}, datetime(2026, 8, 3, 9, 20))
    assert len(result.get("watchlist", [])) >= 1


def test_agent_rules_veto_scalping(calan_config):
    agent = IntradayAgent(calan_config)
    signal = IntradaySignal(
        "RELIANCE",
        "scalping",
        "BUY",
        datetime(2026, 8, 3, 10, 0),
        100.0,
        99.5,
        100.5,
        "test",
    )
    ranked = RankedStock(
        symbol="RELIANCE",
        score=50.0,
        turnover=1e8,
        volume_expansion=2.0,
        range_pct=1.0,
        gap_pct=1.0,
        vwap_distance_pct=0.1,
        prior_level_distance_pct=0.5,
        trend_cleanliness=0.6,
        in_play=True,
    )
    frame = _sample_frame()
    gate = agent.gate_trade(signal, ranked, frame, datetime(2026, 8, 3, 10, 0), 0.0, None, 180.0)
    assert gate["decision"] == "veto"


def test_ranker_allows_volume_in_play_despite_choppy_open(calan_config):
    ranker = IntradayRanker(calan_config)
    idx = pd.date_range("2026-08-03 09:15", periods=48, freq="5min")
    closes = [100.0]
    for i in range(1, len(idx)):
        closes.append(closes[-1] + (0.05 if i % 2 == 0 else -0.04))
    frame = pd.DataFrame(
        {
            "open": closes,
            "high": [c + 0.2 for c in closes],
            "low": [c - 0.2 for c in closes],
            "close": closes,
            "volume": [120_000] * 20 + [320_000] * (len(idx) - 20),
        },
        index=idx,
    )
    ranked = ranker.rank({"RELIANCE": frame}, {"RELIANCE": {"close": 99.0, "high": 100, "low": 98}})
    assert len(ranked) == 1
    assert ranked[0].in_play is True
    assert ranked[0].trend_cleanliness < 0.5


def test_ranker_turnover_only_is_not_in_play(calan_config):
    ranker = IntradayRanker(calan_config)
    idx = pd.date_range("2026-08-03 09:15", periods=48, freq="5min")
    price = 1000.0
    frame = pd.DataFrame(
        {
            "open": [price] * len(idx),
            "high": [price + 5] * len(idx),
            "low": [price - 5] * len(idx),
            "close": [price + (1 if i % 2 else -1) for i in range(len(idx))],
            "volume": [200_000] * len(idx),
        },
        index=idx,
    )
    ranked = ranker.rank(
        {"RELIANCE": frame},
        {"RELIANCE": {"close": price, "high": price + 10, "low": price - 10}},
    )
    assert ranked == [] or ranked[0].in_play is False


def test_gate_rules_veto_even_if_llm_would_approve(calan_config, monkeypatch):
    agent = IntradayAgent(calan_config)
    signal = IntradaySignal(
        "RELIANCE",
        "opening_range_breakout",
        "BUY",
        datetime(2026, 8, 3, 10, 0),
        100.0,
        99.95,
        100.15,
        "test",
    )
    ranked = RankedStock(
        symbol="RELIANCE",
        score=50.0,
        turnover=2e8,
        volume_expansion=2.5,
        range_pct=1.0,
        gap_pct=1.0,
        vwap_distance_pct=0.1,
        prior_level_distance_pct=0.5,
        trend_cleanliness=0.6,
        in_play=True,
    )
    frame = _sample_frame()

    def _fake_llm_approve(*args, **kwargs):
        return {
            "decision": "approve",
            "symbol": "RELIANCE",
            "strategy": "opening_range_breakout",
            "side": "BUY",
            "confidence": 0.9,
            "rationale": "LLM approve",
            "generated_by_llm": True,
        }

    monkeypatch.setattr(agent, "_gate_llm", _fake_llm_approve)
    gate = agent.gate_trade(signal, ranked, frame, datetime(2026, 8, 3, 10, 0), 0.0, None, 180.0)
    assert gate["decision"] == "veto"


def test_simulator_runs_on_synthetic_candles(calan_config):
    frames = {"RELIANCE": _sample_frame()}
    result = IntradaySimulator(calan_config).run(frames, {}, start_date=None, end_date=None)
    assert result.persona_id == "calan"
    assert result.starting_capital == 500_000.0


def test_candles_incremental_refresh_uses_cache(calan_config):
    from datetime import date
    from src.intraday.data import IntradayDataService

    fixed_now = datetime(2026, 8, 3, 10, 30)
    broker = MagicMock()
    service = IntradayDataService(
        calan_config,
        broker=broker,
        now=lambda: fixed_now,
    )
    service._save_cache = lambda: None
    day = date(2026, 8, 3)
    base_idx = pd.date_range("2026-08-03 09:15", periods=12, freq="5min")
    base = pd.DataFrame(
        {
            "open": [100.0] * len(base_idx),
            "high": [101.0] * len(base_idx),
            "low": [99.0] * len(base_idx),
            "close": [100.5] * len(base_idx),
            "volume": [100_000] * len(base_idx),
        },
        index=base_idx,
    )
    service._cache[service._key("RELIANCE", day, "FIVE_MINUTE")] = base

    broker.get_candles.return_value = [
        {
            "timestamp": "2026-08-03 10:30:00",
            "open": 101,
            "high": 102,
            "low": 100,
            "close": 101.5,
            "volume": 120000,
        }
    ]

    frame = service.candles("RELIANCE", day, refresh=True)
    assert len(frame) == 13
    broker.get_candles.assert_called_once()
    args = broker.get_candles.call_args[0]
    assert args[1].hour == 10 and args[1].minute == 5

    broker.get_candles.reset_mock()
    fresh = service.candles("RELIANCE", day, refresh=True)
    assert len(fresh) == 13
    broker.get_candles.assert_not_called()


def test_persona_prompt_bundle_calan(calan_config):
    from src.intraday.prompts import get_persona_prompt_bundle

    bundle = get_persona_prompt_bundle("calan")
    assert "in_play" in bundle.catalyst_prompt
    assert bundle.gate_prompt is not None
    assert "opening_range_breakout" in bundle.gate_strategies


def test_persona_prompt_bundle_revert():
    from src.intraday.prompts import get_persona_prompt_bundle, validate_revert_catalyst_payload

    bundle = get_persona_prompt_bundle("revert")
    assert "session_regime" in bundle.catalyst_prompt
    payload = bundle.validate_catalyst(
        {
            "session_regime": "range",
            "session_view": "test",
            "trade_day_quality": "fair",
            "watchlist": [
                {
                    "symbol": "TCS",
                    "fade_candidate": True,
                    "direction_bias": "long",
                    "confidence": 0.7,
                    "rationale": "test",
                }
            ],
        }
    )
    assert payload is not None
    assert payload["session_regime"] == "range"
    assert payload["watchlist"][0]["in_play"] is True
    assert validate_revert_catalyst_payload(payload) is not None


def test_persona_prompt_bundle_unknown_falls_back_to_calan():
    from src.intraday.prompts import get_persona_prompt_bundle

    bundle = get_persona_prompt_bundle("unknown_persona")
    calan = get_persona_prompt_bundle("calan")
    assert bundle.catalyst_prompt == calan.catalyst_prompt


def test_revert_agent_regime_rules():
    config = get_persona_config("revert")
    config.raw.setdefault("intraday", {})["agent"] = {"enabled": False}
    agent = IntradayAgent(config)
    ranked = [
        RankedStock(
            symbol="TCS",
            score=70.0,
            turnover=2e8,
            volume_expansion=1.2,
            range_pct=0.8,
            gap_pct=0.2,
            vwap_distance_pct=1.5,
            prior_level_distance_pct=0.5,
            trend_cleanliness=0.3,
            in_play=False,
            reasons=["fade"],
        )
    ]
    result = agent.catalyst_scan(ranked, {}, datetime(2026, 8, 3, 10, 30))
    assert result["session_regime"] in ("range", "chop", "trend")
    assert result["generated_by_llm"] is False


def test_list_personas_includes_new_stubs():
    from src.config import list_personas

    personas = list_personas()
    for pid in ("calan", "revert", "flux", "spread", "slice"):
        assert pid in personas


def test_intraday_uses_broker_mode(calan_config):
    from dataclasses import replace

    from src.intraday.broker_execution import (
        entry_order_request,
        exit_order_request,
        intraday_uses_broker,
    )
    from src.intraday.models import PaperPosition

    assert intraday_uses_broker(calan_config) is False
    broker_cfg = replace(
        calan_config,
        raw={**calan_config.raw, "intraday": {**calan_config.raw["intraday"], "mode": "broker"}},
    )
    assert intraday_uses_broker(broker_cfg) is True

    position = PaperPosition(
        symbol="AAPL",
        strategy="opening_range_breakout",
        side="BUY",
        quantity=10,
        entry_time=datetime(2026, 8, 3, 10, 0),
        entry_price=150.0,
        stop_price=149.0,
        target_price=152.0,
        max_exit_time=None,
        reason="test",
    )
    entry = entry_order_request(broker_cfg, position)
    assert entry.side == "BUY"
    assert entry.quantity == 10
    assert entry.price == 150.0

    exit_req = exit_order_request(broker_cfg, position, 151.0)
    assert exit_req.side == "SELL"
    assert exit_req.order_type == "MARKET"


def test_us_personas_use_broker_mode():
    from src.config import get_persona_config
    from src.intraday.broker_execution import intraday_uses_broker

    for pid in ("calan_us", "revert_us", "flux_us", "spread_us", "slice_us"):
        cfg = get_persona_config(pid)
        assert intraday_uses_broker(cfg) is True


def test_broker_executor_submits_entry_and_exit(calan_config):
    from dataclasses import replace

    from src.broker.base import BrokerFunds, OrderResult
    from src.intraday.broker_execution import IntradayBrokerExecutor
    from src.intraday.models import PaperPosition

    broker_cfg = replace(
        calan_config,
        raw={
            **calan_config.raw,
            "connectors": {"broker": "alpaca", "market_region": "US"},
            "execution": {"product_type": "INTRADAY", "exchange": "US"},
            "intraday": {**calan_config.raw["intraday"], "mode": "broker"},
        },
        secrets=replace(
            calan_config.secrets,
            alpaca_api_key="key",
            alpaca_secret_key="secret",
        ),
    )
    broker = MagicMock()
    broker.name = "Alpaca"
    broker.connect.return_value = True
    broker.get_funds.return_value = BrokerFunds(available_cash=50_000, net_value=55_000)
    broker.place_order.side_effect = [
        OrderResult(True, "AAPL", "BUY", 5, order_id="entry-1", status="accepted"),
        OrderResult(True, "AAPL", "SELL", 5, order_id="exit-1", status="filled"),
    ]

    executor = IntradayBrokerExecutor(broker_cfg)
    executor._broker = broker
    executor._connected = True

    position = PaperPosition(
        symbol="AAPL",
        strategy="opening_range_breakout",
        side="BUY",
        quantity=5,
        entry_time=datetime(2026, 8, 3, 10, 0),
        entry_price=150.0,
        stop_price=149.0,
        target_price=152.0,
        max_exit_time=None,
        reason="test",
    )
    ok, err = executor.submit_entry(position)
    assert ok is True
    assert err is None
    assert position.entry_order_id == "entry-1"

    ok, err = executor.submit_exit(position, 151.0)
    assert ok is True
    assert position.exit_order_id == "exit-1"
    assert broker.place_order.call_count == 2


def test_gate_waives_trend_for_gap_catalyst(calan_config):
    from dataclasses import replace

    agent = IntradayAgent(calan_config)
    signal = IntradaySignal(
        "INTC",
        "opening_range_breakout",
        "BUY",
        datetime(2026, 8, 4, 10, 0),
        98.0,
        96.0,
        102.0,
        "test",
    )
    ranked = RankedStock(
        symbol="INTC",
        score=170.0,
        turnover=5e8,
        volume_expansion=1.2,
        range_pct=2.0,
        gap_pct=4.5,
        vwap_distance_pct=0.5,
        prior_level_distance_pct=0.5,
        trend_cleanliness=0.22,
        in_play=True,
    )
    idx = pd.date_range("2026-08-04 09:45", periods=6, freq="5min")
    closes = [97.0, 97.2, 97.5, 98.0, 98.3, 98.5]
    frame = pd.DataFrame(
        {
            "open": closes,
            "high": [c + 0.3 for c in closes],
            "low": [c - 0.3 for c in closes],
            "close": closes,
            "volume": [120_000] * len(idx),
        },
        index=idx,
    )
    gate = agent.gate_trade(signal, ranked, frame, datetime(2026, 8, 4, 10, 0), 0.0, None, 50.0)
    assert "trend at signal" not in gate.get("rationale", "")


def test_trade_confidence_quant_fallback(calan_config):
    agent = IntradayAgent(calan_config)
    ranked = RankedStock(
        symbol="INTC",
        score=170.0,
        turnover=5e8,
        volume_expansion=1.2,
        range_pct=2.0,
        gap_pct=4.5,
        vwap_distance_pct=0.5,
        prior_level_distance_pct=0.5,
        trend_cleanliness=0.22,
        in_play=True,
    )
    conf = agent._trade_confidence(None, ranked)
    assert conf >= 0.55


