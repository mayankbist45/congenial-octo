"""Intraday strategy and simulator tests for Calan."""

from __future__ import annotations

import copy
from dataclasses import replace
from datetime import date, datetime

import pandas as pd

from src.config import Config
from src.intraday.data import IntradayDataService, normalize_candles
from src.intraday.reporting import summarize_result
from src.intraday.ranking import IntradayRanker
from src.intraday.simulator import IntradaySimulator
from src.intraday.models import PaperPosition
from src.intraday.state import PaperStateStore
from src.intraday.strategies import (
    OpeningRangeBreakoutStrategy,
    ScalpingStrategy,
    VwapPullbackStrategy,
)
from src.intraday.watch import IntradayWatchEngine
from scripts.intraday_backtest import _rank_backtest_symbols


def calan_config(temp_config) -> Config:
    raw = copy.deepcopy(temp_config.raw)
    calan = raw["personas"]["calan"]
    for section in ("intraday", "execution", "universe"):
        raw[section] = copy.deepcopy(calan[section])
    secrets = temp_config.secrets.with_capital(float(calan["capital_inr"]))
    return Config(raw=raw, secrets=secrets, root=temp_config.root, persona_id="calan")


def frame(rows) -> pd.DataFrame:
    data = pd.DataFrame(
        rows,
        columns=["timestamp", "open", "high", "low", "close", "volume"],
    )
    data["timestamp"] = pd.to_datetime(data["timestamp"])
    return data.set_index("timestamp")


def test_opening_range_breakout_generates_long_signal(temp_config) -> None:
    config = calan_config(temp_config)
    candles = frame(
        [
            ("2026-01-02 09:30", 100, 101, 99.5, 100.5, 100000),
            ("2026-01-02 09:35", 100.5, 101, 99.8, 100.2, 100000),
            ("2026-01-02 09:40", 100.2, 100.8, 99.7, 100.4, 100000),
            ("2026-01-02 09:45", 100.4, 102, 100.3, 101.5, 200000),
            ("2026-01-02 09:50", 101.5, 103, 101.2, 102.5, 200000),
        ]
    )

    [signal] = OpeningRangeBreakoutStrategy().signals("TCS", candles, config)

    assert signal.side == "BUY"
    assert signal.strategy == "opening_range_breakout"
    assert signal.entry_price == 101.5
    assert signal.stop_price < 100


def test_vwap_chop_is_rejected(temp_config) -> None:
    config = calan_config(temp_config)
    rows = []
    price = 100.0
    for i in range(12):
        ts = pd.Timestamp("2026-01-02 09:30") + pd.Timedelta(minutes=5 * i)
        close = price + (0.4 if i % 2 == 0 else -0.4)
        rows.append((ts, price, max(price, close) + 0.1, min(price, close) - 0.1, close, 100000))
        price = close
    candles = frame(rows)

    signals = VwapPullbackStrategy().signals("TCS", candles, config)

    assert signals == []


def test_scalping_signal_is_skipped_when_edge_cannot_cover_costs(temp_config) -> None:
    config = calan_config(temp_config)
    config.raw["intraday"]["scalping_target_pct"] = 0.2
    candles = frame(
        [
            ("2026-01-02 09:35", 100, 100.1, 99.9, 100.0, 100000),
            ("2026-01-02 09:36", 100, 100.1, 99.9, 100.0, 100000),
            ("2026-01-02 09:37", 100, 100.1, 99.9, 100.0, 100000),
            ("2026-01-02 09:38", 100, 100.6, 100.0, 100.5, 250000),
            ("2026-01-02 09:39", 100.5, 101, 100.4, 100.8, 250000),
            ("2026-01-02 09:40", 100.8, 101.2, 100.7, 101.0, 250000),
        ]
    )

    assert ScalpingStrategy().signals("TCS", candles, config) == []


def test_simulator_forces_time_exit_and_charges_costs(temp_config) -> None:
    config = calan_config(temp_config)
    candles = frame(
        [
            ("2026-01-02 09:30", 100, 101, 99.5, 100.5, 500000),
            ("2026-01-02 09:35", 100.5, 101, 99.8, 100.2, 500000),
            ("2026-01-02 09:40", 100.2, 100.8, 99.7, 100.4, 500000),
            ("2026-01-02 09:45", 100.4, 102, 100.3, 101.5, 900000),
            ("2026-01-02 09:50", 101.5, 101.8, 101.2, 101.4, 500000),
            ("2026-01-02 15:15", 101.5, 101.7, 101.3, 101.6, 500000),
        ]
    )

    result = IntradaySimulator(config, strategies=[OpeningRangeBreakoutStrategy()]).run(
        {"TCS": candles}, {}, date(2026, 1, 2), date(2026, 1, 2)
    )
    [trade] = result.trades

    assert trade.exit_reason == "time_exit"
    assert trade.costs > 0
    assert trade.net_pnl < trade.gross_pnl
    assert result.ending_capital == config.secrets.initial_capital + trade.net_pnl


def test_backtest_summary_reports_expectancy_and_attribution(temp_config) -> None:
    config = calan_config(temp_config)
    candles = frame(
        [
            ("2026-01-02 09:30", 100, 101, 99.5, 100.5, 500000),
            ("2026-01-02 09:35", 100.5, 101, 99.8, 100.2, 500000),
            ("2026-01-02 09:40", 100.2, 100.8, 99.7, 100.4, 500000),
            ("2026-01-02 09:45", 100.4, 102, 100.3, 101.5, 900000),
            ("2026-01-02 09:50", 101.5, 105, 101.2, 104.5, 900000),
            ("2026-01-02 09:55", 104.5, 106, 104.2, 105.5, 900000),
        ]
    )

    result = IntradaySimulator(config, strategies=[OpeningRangeBreakoutStrategy()]).run(
        {"TCS": candles}, {}, date(2026, 1, 2), date(2026, 1, 2)
    )
    summary = summarize_result(result)

    assert summary["total_trades"] == 1
    assert summary["win_rate_pct"] == 100.0
    assert "opening_range_breakout" in summary["strategy_pnl"]


def test_backtest_symbol_selection_ranks_instead_of_alphabetical(temp_config) -> None:
    config = calan_config(temp_config)
    dull_alpha = frame(
        [
            ("2026-01-02 09:30", 100, 100.1, 99.9, 100.0, 10000),
            ("2026-01-02 09:35", 100, 100.1, 99.9, 100.0, 10000),
            ("2026-01-02 09:40", 100, 100.1, 99.9, 100.0, 10000),
            ("2026-01-02 09:45", 100, 100.1, 99.9, 100.0, 10000),
            ("2026-01-02 09:50", 100, 100.1, 99.9, 100.0, 10000),
            ("2026-01-02 09:55", 100, 100.1, 99.9, 100.0, 10000),
        ]
    )
    active_late = frame(
        [
            ("2026-01-02 09:30", 100, 101, 99.5, 100.5, 500000),
            ("2026-01-02 09:35", 100.5, 102, 100.4, 101.8, 600000),
            ("2026-01-02 09:40", 101.8, 103, 101.7, 102.8, 700000),
            ("2026-01-02 09:45", 102.8, 104, 102.5, 103.8, 800000),
            ("2026-01-02 09:50", 103.8, 105, 103.2, 104.7, 900000),
            ("2026-01-02 09:55", 104.7, 106, 104.5, 105.8, 1000000),
        ]
    )

    selected = _rank_backtest_symbols(
        config,
        {"AAA": dull_alpha, "ZZZ": active_late},
        {},
        limit=1,
    )

    assert selected == ["ZZZ"]


def test_ranker_prefers_active_trader_friendly_stock(temp_config) -> None:
    config = calan_config(temp_config)
    active = frame(
        [
            ("2026-01-02 09:30", 100, 101, 99.5, 100.5, 500000),
            ("2026-01-02 09:35", 100.5, 102, 100.4, 101.8, 600000),
            ("2026-01-02 09:40", 101.8, 103, 101.7, 102.8, 700000),
            ("2026-01-02 09:45", 102.8, 104, 102.5, 103.8, 800000),
            ("2026-01-02 09:50", 103.8, 105, 103.2, 104.7, 900000),
            ("2026-01-02 09:55", 104.7, 106, 104.5, 105.8, 1000000),
        ]
    )
    dull = frame(
        [
            ("2026-01-02 09:30", 100, 100.1, 99.9, 100.0, 10000),
            ("2026-01-02 09:35", 100, 100.1, 99.9, 100.0, 10000),
            ("2026-01-02 09:40", 100, 100.1, 99.9, 100.0, 10000),
            ("2026-01-02 09:45", 100, 100.1, 99.9, 100.0, 10000),
            ("2026-01-02 09:50", 100, 100.1, 99.9, 100.0, 10000),
            ("2026-01-02 09:55", 100, 100.1, 99.9, 100.0, 10000),
        ]
    )

    ranked = IntradayRanker(config).rank({"ACTIVE": active, "DULL": dull})

    assert [item.symbol for item in ranked] == ["ACTIVE"]
    assert ranked[0].score > 0


def test_ranker_rejects_excessively_choppy_vwap_names(temp_config) -> None:
    config = calan_config(temp_config)
    trending = frame(
        [
            ("2026-01-02 09:30", 100, 101, 99.5, 100.5, 500000),
            ("2026-01-02 09:35", 100.5, 102, 100.4, 101.5, 500000),
            ("2026-01-02 09:40", 101.5, 103, 101.3, 102.5, 500000),
            ("2026-01-02 09:45", 102.5, 104, 102.1, 103.5, 500000),
            ("2026-01-02 09:50", 103.5, 105, 103.2, 104.5, 500000),
            ("2026-01-02 09:55", 104.5, 106, 104.1, 105.5, 500000),
        ]
    )
    choppy = frame(
        [
            ("2026-01-02 09:30", 100, 102, 99, 101, 500000),
            ("2026-01-02 09:35", 101, 102, 99, 100.4, 500000),
            ("2026-01-02 09:40", 100.4, 103, 100, 101.2, 500000),
            ("2026-01-02 09:45", 101.2, 103, 100, 100.8, 500000),
            ("2026-01-02 09:50", 100.8, 104, 100.4, 101.6, 500000),
            ("2026-01-02 09:55", 101.6, 104, 100.8, 101.1, 500000),
        ]
    )

    ranked = IntradayRanker(config).rank({"TREND": trending, "CHOP": choppy})

    assert [item.symbol for item in ranked] == ["TREND"]


def test_paper_state_store_recovers_from_missing_and_corrupt_files(temp_config, tmp_path) -> None:
    config = calan_config(temp_config)
    path = tmp_path / "state.json"
    store = PaperStateStore(config, path=path)

    state = store.load(date(2026, 1, 2))
    assert state.open_positions == []

    path.write_text("{not-json", encoding="utf-8")
    state = store.load(date(2026, 1, 2))
    assert state.completed_trades == []


class RecordingCandleBroker:
    name = "recording"

    def __init__(self):
        self.calls = []

    def connect(self):
        return True

    def get_holdings(self):
        return []

    def get_funds(self):
        return None

    def get_last_price(self, symbol):
        return None

    def get_last_prices(self, symbols):
        return {}

    def place_order(self, request):
        return None

    def get_candles(self, symbol, from_date, to_date, interval="FIVE_MINUTE"):
        self.calls.append((symbol, from_date, to_date, interval))
        return []


def test_intraday_data_skips_today_before_market_open(temp_config) -> None:
    config = calan_config(temp_config)
    broker = RecordingCandleBroker()
    service = IntradayDataService(
        config,
        broker=broker,
        now=lambda: datetime(2026, 8, 3, 4, 2),
    )

    result = service.candles("TCS", date(2026, 8, 3), use_cache=False)

    assert result.empty
    assert broker.calls == []


def test_intraday_data_clamps_today_to_current_time(temp_config) -> None:
    config = calan_config(temp_config)
    broker = RecordingCandleBroker()
    service = IntradayDataService(
        config,
        broker=broker,
        now=lambda: datetime(2026, 8, 3, 10, 17),
    )

    service.candles("TCS", date(2026, 8, 3), use_cache=False)

    assert len(broker.calls) == 1
    _, from_date, to_date, _ = broker.calls[0]
    assert from_date == datetime(2026, 8, 3, 9, 15)
    assert to_date == datetime(2026, 8, 3, 10, 17)


def test_intraday_data_skips_weekends(temp_config) -> None:
    config = calan_config(temp_config)
    broker = RecordingCandleBroker()
    service = IntradayDataService(
        config,
        broker=broker,
        now=lambda: datetime(2026, 8, 3, 10, 17),
    )

    result = service.candles("TCS", date(2026, 8, 2), use_cache=False)

    assert result.empty
    assert broker.calls == []


def test_normalize_candles_strips_timezone_from_angel_timestamps() -> None:
    candles = normalize_candles(
        [
            {
                "timestamp": "2026-07-27T09:15:00+05:30",
                "open": 100,
                "high": 101,
                "low": 99,
                "close": 100.5,
                "volume": 100000,
            }
        ]
    )

    assert candles.index.tz is None
    assert candles.index[0] == pd.Timestamp("2026-07-27 09:15:00")


class FakeIntradayData:
    def __init__(self, frames):
        self.frames = frames

    def candles(self, symbol, day, interval="FIVE_MINUTE", use_cache=True):
        return self.frames.get(symbol, frame([]))


def test_watch_poll_scans_small_rotating_slice(temp_config, tmp_path) -> None:
    config = calan_config(temp_config)
    config.raw.setdefault("intraday", {})
    config.raw["intraday"]["watch_scan_size"] = 3
    config.raw["intraday"]["watch_poll_seconds"] = 60
    store = PaperStateStore(config, path=tmp_path / "state.json")
    engine = IntradayWatchEngine(
        config,
        FakeIntradayData({}),
        ["AAA", "BBB", "CCC", "DDD", "EEE"],
        state_store=store,
    )
    state = store.load(date(2026, 1, 2))
    state.open_positions = [
        PaperPosition(
            symbol="EEE",
            strategy="orb",
            side="BUY",
            quantity=1,
            entry_time=datetime(2026, 1, 2, 10, 0),
            entry_price=100.0,
            stop_price=99.0,
            target_price=102.0,
        )
    ]

    symbols = engine._symbols_for_poll(state, datetime(2026, 1, 2, 10, 5))

    assert symbols[0] == "EEE"
    assert len(symbols) == 4  # open position + 3 scan names
    assert len(set(symbols)) == 4


def test_watch_loop_does_not_enter_before_trade_window(temp_config, tmp_path) -> None:
    config = calan_config(temp_config)
    candles = frame(
        [
            ("2026-01-02 09:15", 100, 101, 99.5, 100.5, 500000),
            ("2026-01-02 09:20", 100.5, 101, 99.8, 100.2, 500000),
            ("2026-01-02 09:25", 100.2, 100.8, 99.7, 100.4, 500000),
        ]
    )
    store = PaperStateStore(config, path=tmp_path / "state.json")
    engine = IntradayWatchEngine(
        config,
        FakeIntradayData({"TCS": candles}),
        ["TCS"],
        state_store=store,
    )

    state = engine.run_once(datetime(2026, 1, 2, 9, 25))

    assert state.open_positions == []
    assert state.completed_trades == []


def test_watch_loop_enters_and_exits_paper_trade(temp_config, tmp_path) -> None:
    config = calan_config(temp_config)
    candles = frame(
        [
            ("2026-01-02 09:15", 100, 100.4, 99.8, 100.1, 500000),
            ("2026-01-02 09:20", 100.1, 100.5, 99.9, 100.2, 500000),
            ("2026-01-02 09:25", 100.2, 100.6, 100.0, 100.3, 500000),
            ("2026-01-02 09:30", 100, 101, 99.5, 100.5, 500000),
            ("2026-01-02 09:35", 100.5, 101, 99.8, 100.2, 500000),
            ("2026-01-02 09:40", 100.2, 100.8, 99.7, 100.4, 500000),
            ("2026-01-02 09:45", 100.4, 102, 100.3, 101.5, 900000),
            ("2026-01-02 09:50", 101.5, 106, 101.2, 105.0, 900000),
        ]
    )
    store = PaperStateStore(config, path=tmp_path / "state.json")
    engine = IntradayWatchEngine(
        config,
        FakeIntradayData({"TCS": candles}),
        ["TCS"],
        state_store=store,
    )

    state = engine.run_once(datetime(2026, 1, 2, 9, 45))
    assert len(state.open_positions) == 1

    state = engine.run_once(datetime(2026, 1, 2, 9, 50))
    assert state.open_positions == []
    assert len(state.completed_trades) == 1
    assert state.daily_pnl != 0


def test_watch_loop_blocks_new_entries_after_trade_window(temp_config, tmp_path) -> None:
    config = calan_config(temp_config)
    candles = frame(
        [
            ("2026-01-02 14:45", 100, 101, 99.5, 100.5, 500000),
            ("2026-01-02 14:50", 100.5, 101, 99.8, 100.2, 500000),
            ("2026-01-02 14:55", 100.2, 100.8, 99.7, 100.4, 500000),
            ("2026-01-02 15:00", 100.4, 102, 100.3, 101.5, 900000),
            ("2026-01-02 15:05", 101.5, 106, 101.2, 105.0, 900000),
        ]
    )
    store = PaperStateStore(config, path=tmp_path / "state.json")
    engine = IntradayWatchEngine(
        config,
        FakeIntradayData({"TCS": candles}),
        ["TCS"],
        state_store=store,
    )

    state = engine.run_once(datetime(2026, 1, 2, 15, 5))

    assert state.open_positions == []
