"""Rule-based intraday strategy playbooks."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Dict, List, Optional

import pandas as pd

from src.config import Config
from src.intraday.models import IntradaySignal


def _cfg_float(config: Config, key: str, default: float) -> float:
    return float(config.get(f"intraday.{key}", default) or default)


def _cfg_int(config: Config, key: str, default: int) -> int:
    return int(config.get(f"intraday.{key}", default) or default)


def _parse_time(value: str) -> time:
    hour, minute = value.split(":", 1)
    return time(int(hour), int(minute))


def _session(frame: pd.DataFrame, config: Config) -> pd.DataFrame:
    start = _parse_time(str(config.get("intraday.trade_window_start", "09:30")))
    end = _parse_time(str(config.get("intraday.force_exit_time", "15:15")))
    return frame.between_time(start, end)


def _with_vwap(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    typical = (out["high"] + out["low"] + out["close"]) / 3.0
    volume = out["volume"].clip(lower=0)
    denom = volume.cumsum().replace(0, pd.NA)
    out["vwap"] = (typical * volume).cumsum() / denom
    return out


def _avg_volume(frame: pd.DataFrame, lookback: int = 12) -> float:
    values = frame["volume"].tail(lookback)
    if values.empty:
        return 0.0
    return float(values.mean())


def _force_exit_dt(ts: datetime, config: Config) -> datetime:
    return datetime.combine(
        ts.date(), _parse_time(str(config.get("intraday.force_exit_time", "15:15")))
    )


class IntradayStrategy:
    name = "base"

    def signals(
        self,
        symbol: str,
        frame: pd.DataFrame,
        config: Config,
        previous_day: Optional[Dict[str, float]] = None,
    ) -> List[IntradaySignal]:
        raise NotImplementedError


class OpeningRangeBreakoutStrategy(IntradayStrategy):
    name = "opening_range_breakout"

    def signals(self, symbol: str, frame: pd.DataFrame, config: Config, previous_day=None) -> List[IntradaySignal]:
        data = _session(frame, config)
        bars = max(_cfg_int(config, "opening_range_minutes", 15) // 5, 1)
        if len(data) <= bars:
            return []

        opening = data.iloc[:bars]
        rest = data.iloc[bars:]
        high = float(opening["high"].max())
        low = float(opening["low"].min())
        if high <= low:
            return []

        volume_multiple = _cfg_float(config, "volume_spike_multiple", 1.2)
        stop_buffer = _cfg_float(config, "stop_buffer_pct", 0.05) / 100.0
        target_r = _cfg_float(config, "target_r_multiple", 1.5)

        for ts, row in rest.iterrows():
            avg_vol = _avg_volume(data.loc[:ts].iloc[:-1])
            if avg_vol > 0 and float(row["volume"]) < avg_vol * volume_multiple:
                continue
            close = float(row["close"])
            if close > high:
                stop = low * (1.0 - stop_buffer)
                target = close + (close - stop) * target_r
                return [IntradaySignal(symbol, self.name, "BUY", ts.to_pydatetime(), close, stop, target, "Opening range high broke on volume", _force_exit_dt(ts.to_pydatetime(), config))]
            if close < low:
                stop = high * (1.0 + stop_buffer)
                target = close - (stop - close) * target_r
                return [IntradaySignal(symbol, self.name, "SELL", ts.to_pydatetime(), close, stop, target, "Opening range low broke on volume", _force_exit_dt(ts.to_pydatetime(), config))]
        return []


class VwapPullbackStrategy(IntradayStrategy):
    name = "vwap_pullback"

    def signals(self, symbol: str, frame: pd.DataFrame, config: Config, previous_day=None) -> List[IntradaySignal]:
        data = _with_vwap(_session(frame, config))
        if len(data) < 8:
            return []

        target_r = _cfg_float(config, "target_r_multiple", 1.5)
        # Skip chop: too many VWAP crosses means no clean institutional trend.
        side_of_vwap = (data["close"] > data["vwap"]).astype(int)
        crosses = int(side_of_vwap.diff().abs().fillna(0).sum())
        if crosses > 5:
            return []

        for i in range(4, len(data)):
            prior = data.iloc[i - 4 : i]
            row = data.iloc[i]
            ts = data.index[i].to_pydatetime()
            close = float(row["close"])
            vwap = float(row["vwap"])
            trend_up = bool((prior["close"] > prior["vwap"]).all())
            trend_down = bool((prior["close"] < prior["vwap"]).all())
            near_vwap = abs(close / vwap - 1.0) <= 0.003 if vwap else False
            if trend_up and near_vwap and close > float(prior["close"].iloc[-1]):
                stop = min(float(row["low"]), vwap) * 0.999
                target = close + (close - stop) * target_r
                return [IntradaySignal(symbol, self.name, "BUY", ts, close, stop, target, "VWAP pullback resumed upward", _force_exit_dt(ts, config))]
            if trend_down and near_vwap and close < float(prior["close"].iloc[-1]):
                stop = max(float(row["high"]), vwap) * 1.001
                target = close - (stop - close) * target_r
                return [IntradaySignal(symbol, self.name, "SELL", ts, close, stop, target, "VWAP pullback resumed downward", _force_exit_dt(ts, config))]
        return []


class PreviousDayBreakStrategy(IntradayStrategy):
    name = "previous_day_break"

    def signals(self, symbol: str, frame: pd.DataFrame, config: Config, previous_day=None) -> List[IntradaySignal]:
        if not previous_day:
            return []
        high = previous_day.get("high")
        low = previous_day.get("low")
        if not high or not low:
            return []
        data = _session(frame, config)
        if len(data) < 3:
            return []

        target_r = _cfg_float(config, "target_r_multiple", 1.5)
        volume_multiple = _cfg_float(config, "volume_spike_multiple", 1.2)
        for ts, row in data.iloc[2:].iterrows():
            avg_vol = _avg_volume(data.loc[:ts].iloc[:-1])
            if avg_vol > 0 and float(row["volume"]) < avg_vol * volume_multiple:
                continue
            close = float(row["close"])
            if close > high:
                stop = float(high) * 0.999
                target = close + (close - stop) * target_r
                return [IntradaySignal(symbol, self.name, "BUY", ts.to_pydatetime(), close, stop, target, "Previous day high broke and held", _force_exit_dt(ts.to_pydatetime(), config))]
            if close < low:
                stop = float(low) * 1.001
                target = close - (stop - close) * target_r
                return [IntradaySignal(symbol, self.name, "SELL", ts.to_pydatetime(), close, stop, target, "Previous day low broke and held", _force_exit_dt(ts.to_pydatetime(), config))]
        return []


class ScalpingStrategy(IntradayStrategy):
    name = "scalping"

    def signals(self, symbol: str, frame: pd.DataFrame, config: Config, previous_day=None) -> List[IntradaySignal]:
        data = _session(frame, config)
        if len(data) < 6:
            return []
        start = datetime.combine(data.index[0].date(), time(9, 35))
        end = datetime.combine(data.index[0].date(), _parse_time(str(config.get("intraday.trade_window_end", "14:45"))))
        target_pct = _cfg_float(config, "scalping_target_pct", 0.25) / 100.0
        stop_pct = _cfg_float(config, "scalping_stop_pct", 0.18) / 100.0
        min_edge = _cfg_float(config, "scalping_min_net_edge_bps", 8.0) / 10000.0
        slippage_round_trip = _cfg_float(config, "slippage_bps_per_side", 2.0) * 2 / 10000.0
        hold = timedelta(minutes=_cfg_int(config, "scalping_max_hold_minutes", 15))
        if target_pct <= stop_pct + min_edge + slippage_round_trip:
            return []

        for i in range(3, len(data)):
            ts = data.index[i].to_pydatetime()
            if ts < start or ts > end:
                continue
            prev = data.iloc[i - 3 : i]
            row = data.iloc[i]
            close = float(row["close"])
            avg_vol = _avg_volume(data.iloc[:i])
            if avg_vol > 0 and float(row["volume"]) < avg_vol * 1.5:
                continue
            if close > float(prev["high"].max()):
                return [IntradaySignal(symbol, self.name, "BUY", ts, close, close * (1.0 - stop_pct), close * (1.0 + target_pct), "Momentum scalp cleared recent highs", min(ts + hold, _force_exit_dt(ts, config)))]
            if close < float(prev["low"].min()):
                return [IntradaySignal(symbol, self.name, "SELL", ts, close, close * (1.0 + stop_pct), close * (1.0 - target_pct), "Momentum scalp cleared recent lows", min(ts + hold, _force_exit_dt(ts, config)))]
        return []


DEFAULT_STRATEGIES = [
    OpeningRangeBreakoutStrategy(),
    VwapPullbackStrategy(),
    PreviousDayBreakStrategy(),
    ScalpingStrategy(),
]
