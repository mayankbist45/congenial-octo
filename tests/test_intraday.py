"""Tests for calan SIP-ORB-VWAP intraday module."""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest
from unittest.mock import MagicMock

from src.config import Config, get_persona_config
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


def test_load_strategies_includes_scalping_for_calan(calan_config):
    names = [s.name for s in load_strategies(calan_config)]
    assert names == ["scalping"]


def test_scalping_strategy_respects_veto_flag(calan_config):
    vetoed = calan_config.raw.copy()
    vetoed.setdefault("intraday", {}).setdefault("agent", {})["veto_scalping"] = True
    veto_config = Config(
        raw=vetoed,
        secrets=calan_config.secrets,
        root=calan_config.root,
        persona_id=calan_config.persona_id,
    )
    assert ScalpingStrategy().signals("TEST", _sample_frame(), veto_config) == []


def test_agent_rules_veto_scalping_when_disabled(calan_config):
    vetoed = calan_config.raw.copy()
    vetoed.setdefault("intraday", {}).setdefault("agent", {})["veto_scalping"] = True
    config = Config(
        raw=vetoed,
        secrets=calan_config.secrets,
        root=calan_config.root,
        persona_id=calan_config.persona_id,
    )
    agent = IntradayAgent(config)
    signal = IntradaySignal(
        "RELIANCE",
        "scalping",
        "BUY",
        datetime(2026, 8, 3, 10, 0),
        100.0,
        99.85,
        100.50,
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
    gate = agent.gate_trade(
        signal, ranked, frame, datetime(2026, 8, 3, 10, 0), 0.0, {"confidence": 0.7}, 180.0
    )
    assert gate["decision"] == "veto"


def test_agent_rules_approve_scalping_when_enabled(calan_config):
    agent = IntradayAgent(calan_config)
    agent._llm = MagicMock()
    agent._llm.available = False
    signal = IntradaySignal(
        "RELIANCE",
        "scalping",
        "BUY",
        datetime(2026, 8, 3, 10, 0),
        100.0,
        99.85,
        100.50,
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
    gate = agent.gate_trade(
        signal,
        ranked,
        frame,
        datetime(2026, 8, 3, 10, 0),
        0.0,
        {"confidence": 0.7},
        180.0,
    )
    assert gate["decision"] == "approve"
    assert gate["use_trailing_stop"] is False


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


def test_previous_day_stats_session_cache(calan_config):
    from datetime import date
    from src.intraday.data import IntradayDataService

    broker = MagicMock()
    broker.get_candles.return_value = [
        {
            "timestamp": "2026-08-05",
            "open": 100,
            "high": 102,
            "low": 99,
            "close": 101,
            "volume": 1000,
        }
    ]
    service = IntradayDataService(calan_config, broker=broker)
    day = date(2026, 8, 6)

    first = service.previous_day_stats("RELIANCE", day)
    second = service.previous_day_stats("RELIANCE", day)

    assert first == second
    assert first["close"] == 101.0
    broker.get_candles.assert_called_once()


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


def test_symbols_for_poll_includes_watchlist(calan_config):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from src.intraday.models import PaperState
    from src.intraday.watch import IntradayWatchEngine

    engine = IntradayWatchEngine(
        calan_config,
        data_service=MagicMock(),
        symbols=["AAPL", "MSFT", "NVDA", "GOOGL", "AMD", "META"],
    )
    state = PaperState(
        persona_id="calan",
        trading_date=datetime(2026, 8, 5).date(),
        watchlist=[
            {"symbol": "AMD", "in_play": True},
            {"symbol": "GOOGL", "in_play": True},
        ],
    )
    now = datetime(2026, 8, 5, 11, 0, tzinfo=ZoneInfo("America/New_York"))
    polled = engine._symbols_for_poll(state, now)
    assert "AMD" in polled
    assert "GOOGL" in polled


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


def test_naive_exchange_dt_unifies_aware_and_naive():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from src.intraday.watch import _naive_exchange_dt

    tz = ZoneInfo("America/New_York")
    aware = datetime(2026, 8, 5, 11, 0, tzinfo=tz)
    naive = datetime(2026, 8, 5, 10, 30)
    assert _naive_exchange_dt(aware, tz) >= _naive_exchange_dt(naive, tz)


def test_exit_for_position_handles_aware_now(calan_config):
    from datetime import datetime
    from unittest.mock import MagicMock
    from zoneinfo import ZoneInfo

    import pandas as pd

    from src.intraday.models import PaperPosition
    from src.intraday.watch import IntradayWatchEngine

    calan_config.raw.setdefault("connectors", {})["market_region"] = "US"
    engine = IntradayWatchEngine(
        calan_config,
        data_service=MagicMock(),
        symbols=["AAPL"],
    )
    position = PaperPosition(
        symbol="AAPL",
        strategy="scalping",
        side="BUY",
        quantity=10,
        entry_time=datetime(2026, 8, 5, 10, 0),
        entry_price=100.0,
        stop_price=99.0,
        target_price=101.0,
        max_exit_time=datetime(2026, 8, 5, 10, 15),
        reason="test",
    )
    frame = pd.DataFrame(
        {
            "open": [100.0],
            "high": [100.5],
            "low": [99.5],
            "close": [100.2],
            "volume": [1000],
        },
        index=pd.DatetimeIndex([datetime(2026, 8, 5, 10, 5)]),
    )
    now = datetime(2026, 8, 5, 10, 20, tzinfo=ZoneInfo("America/New_York"))
    result = engine._exit_for_position(position, frame, now)
    assert result is not None
    assert result["exit_reason"] in ("max_hold", "paper_time_exit_no_candle")


def test_watch_stale_signal_is_rejected(calan_config):
    from zoneinfo import ZoneInfo

    from src.intraday.watch import IntradayWatchEngine

    us_config = get_persona_config("calan_us")
    engine = IntradayWatchEngine(
        us_config,
        data_service=MagicMock(),
        symbols=["AAPL"],
    )
    now = datetime(2026, 8, 5, 11, 30, tzinfo=ZoneInfo("America/New_York"))
    stale = IntradaySignal(
        "AAPL",
        "opening_range_breakout",
        "BUY",
        datetime(2026, 8, 5, 10, 0),
        100.0,
        99.0,
        102.0,
        "test",
    )
    assert engine._is_stale_signal(stale, now) is True

    fresh = IntradaySignal(
        "AAPL",
        "opening_range_breakout",
        "BUY",
        datetime(2026, 8, 5, 11, 28),
        100.0,
        99.0,
        102.0,
        "test",
    )
    assert engine._is_stale_signal(fresh, now) is False


def test_watch_skips_symbol_not_on_watchlist(calan_config):
    from zoneinfo import ZoneInfo

    from src.intraday.models import PaperState
    from src.intraday.watch import IntradayWatchEngine

    engine = IntradayWatchEngine(
        calan_config,
        data_service=MagicMock(),
        symbols=["AAPL", "AMD", "GOOGL"],
        agent=MagicMock(),
    )
    engine._broker_executor = MagicMock(return_value=None)
    state = PaperState(
        persona_id="calan",
        trading_date=datetime(2026, 8, 5).date(),
        watchlist=[{"symbol": "GOOGL", "in_play": True, "confidence": 0.8}],
    )
    amd_signal = IntradaySignal(
        "AMD",
        "opening_range_breakout",
        "SELL",
        datetime(2026, 8, 5, 11, 0),
        100.0,
        101.0,
        98.0,
        "ORB low break with volume below VWAP",
    )
    ranked = [
        RankedStock(
            symbol="AMD",
            score=180.0,
            turnover=1e9,
            volume_expansion=2.0,
            range_pct=2.0,
            gap_pct=1.5,
            vwap_distance_pct=0.5,
            prior_level_distance_pct=0.5,
            trend_cleanliness=0.5,
            in_play=True,
        )
    ]
    engine._rank_signals = MagicMock(return_value=[(180.0, amd_signal)])
    frames = {"AMD": _sample_frame()}
    now = datetime(2026, 8, 5, 11, 0, tzinfo=ZoneInfo("America/New_York"))
    engine._open_new_positions(state, frames, ranked, now, {})
    assert any(s["reason"] == "symbol not on watchlist" for s in state.skipped_signals)


def test_filter_ranked_for_persona_includes_calan_us():
    from src.intraday.persona_rankers import filter_ranked_for_persona

    config = get_persona_config("calan_us")
    ranked = [
        RankedStock(
            symbol="AMD",
            score=180.0,
            turnover=1e9,
            volume_expansion=2.0,
            range_pct=2.0,
            gap_pct=1.5,
            vwap_distance_pct=0.5,
            prior_level_distance_pct=0.5,
            trend_cleanliness=0.5,
            in_play=True,
        ),
        RankedStock(
            symbol="INTC",
            score=120.0,
            turnover=1e8,
            volume_expansion=0.5,
            range_pct=1.0,
            gap_pct=0.2,
            vwap_distance_pct=0.1,
            prior_level_distance_pct=0.1,
            trend_cleanliness=0.2,
            in_play=False,
        ),
    ]
    filtered = filter_ranked_for_persona(config, ranked)
    assert [r.symbol for r in filtered] == ["AMD"]


def test_catalyst_watchlist_defaults_in_play_true():
    from src.intraday.prompts import validate_catalyst_payload

    payload = validate_catalyst_payload(
        {
            "session_view": "test",
            "trade_day_quality": "fair",
            "watchlist": [
                {
                    "symbol": "GOOGL",
                    "direction_bias": "long",
                    "confidence": 0.7,
                    "rationale": "gap",
                    "catalyst_class": "technical_only",
                }
            ],
        }
    )
    assert payload is not None
    assert payload["watchlist"][0]["in_play"] is True

