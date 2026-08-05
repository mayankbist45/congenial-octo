"""Rule-based intraday playbooks (ORB + VWAP; scalping optional)."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Dict, List, Optional

import pandas as pd

from src.config import Config
from src.intraday.costs import estimate_signal_quantity, passes_min_net_edge
from src.intraday.cross_stat_math import configured_cross_baskets, hedge_beta, residual_z_history
from src.intraday.models import IntradaySignal
from src.intraday.ofi import ofi_divergence, ofi_z_proxy
from src.intraday.spread_math import configured_pairs, pair_beta, spread_z_history

STRATEGY_REGISTRY: Dict[str, type] = {}


def _register(cls: type) -> type:
    STRATEGY_REGISTRY[cls.name] = cls
    return cls


def _cfg_float(config: Config, key: str, default: float) -> float:
    return float(config.get(f"intraday.{key}", default) or default)


def _cfg_int(config: Config, key: str, default: int) -> int:
    return int(config.get(f"intraday.{key}", default) or default)


def _parse_time(value: str) -> time:
    hour, minute = value.split(":", 1)
    return time(int(hour), int(minute))


def _session(frame: pd.DataFrame, config: Config) -> pd.DataFrame:
    start = _parse_time(str(config.get("intraday.trade_window_start", "09:45")))
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


@_register
class OpeningRangeBreakoutStrategy(IntradayStrategy):
    name = "opening_range_breakout"

    def signals(
        self,
        symbol: str,
        frame: pd.DataFrame,
        config: Config,
        previous_day=None,
    ) -> List[IntradaySignal]:
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
        min_or_range = _cfg_float(config, "orb_min_range_pct", 0.0)
        min_or_trend = _cfg_float(config, "orb_min_trend_at_signal", 0.0)

        for ts, row in rest.iterrows():
            avg_vol = _avg_volume(data.loc[:ts].iloc[:-1])
            if avg_vol > 0 and float(row["volume"]) < avg_vol * volume_multiple:
                continue
            close = float(row["close"])
            vwap_frame = _with_vwap(data.loc[:ts])
            vwap = float(vwap_frame["vwap"].iloc[-1]) if not vwap_frame.empty else close
            if min_or_range > 0 and low > 0:
                or_range_pct = (high / low - 1.0) * 100.0
                if or_range_pct < min_or_range:
                    continue
            if min_or_trend > 0:
                from src.intraday.ranking import IntradayRanker

                trend = IntradayRanker.trend_cleanliness(
                    frame, config, pd.Timestamp(ts.to_pydatetime())
                )
                if trend < min_or_trend:
                    continue
            if close > high and close >= vwap:
                stop = low * (1.0 - stop_buffer)
                target = close + (close - stop) * target_r
                return [
                    IntradaySignal(
                        symbol,
                        self.name,
                        "BUY",
                        ts.to_pydatetime(),
                        close,
                        stop,
                        target,
                        "ORB high break with volume above VWAP",
                        _force_exit_dt(ts.to_pydatetime(), config),
                    )
                ]
            if close < low and close <= vwap:
                stop = high * (1.0 + stop_buffer)
                target = close - (stop - close) * target_r
                return [
                    IntradaySignal(
                        symbol,
                        self.name,
                        "SELL",
                        ts.to_pydatetime(),
                        close,
                        stop,
                        target,
                        "ORB low break with volume below VWAP",
                        _force_exit_dt(ts.to_pydatetime(), config),
                    )
                ]
        return []


@_register
class VwapPullbackStrategy(IntradayStrategy):
    name = "vwap_pullback"

    def signals(
        self,
        symbol: str,
        frame: pd.DataFrame,
        config: Config,
        previous_day=None,
    ) -> List[IntradaySignal]:
        data = _with_vwap(_session(frame, config))
        if len(data) < 8:
            return []

        target_r = _cfg_float(config, "target_r_multiple", 1.5)
        side_of_vwap = (data["close"] > data["vwap"]).astype(int)
        crosses = int(side_of_vwap.diff().abs().fillna(0).sum())
        if crosses > 4:
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
                return [
                    IntradaySignal(
                        symbol,
                        self.name,
                        "BUY",
                        ts,
                        close,
                        stop,
                        target,
                        "VWAP pullback resumed upward",
                        _force_exit_dt(ts, config),
                    )
                ]
            if trend_down and near_vwap and close < float(prior["close"].iloc[-1]):
                stop = max(float(row["high"]), vwap) * 1.001
                target = close - (stop - close) * target_r
                return [
                    IntradaySignal(
                        symbol,
                        self.name,
                        "SELL",
                        ts,
                        close,
                        stop,
                        target,
                        "VWAP pullback resumed downward",
                        _force_exit_dt(ts, config),
                    )
                ]
        return []


@_register
class ScalpingStrategy(IntradayStrategy):
    name = "scalping"

    def signals(
        self,
        symbol: str,
        frame: pd.DataFrame,
        config: Config,
        previous_day=None,
    ) -> List[IntradaySignal]:
        if config.get("intraday.agent.veto_scalping", True):
            return []
        data = _session(frame, config)
        if len(data) < 6:
            return []
        start = datetime.combine(data.index[0].date(), time(10, 15))
        end = datetime.combine(
            data.index[0].date(),
            _parse_time(str(config.get("intraday.trade_window_end", "14:30"))),
        )
        target_pct = _cfg_float(config, "scalping_target_pct", 0.50) / 100.0
        stop_pct = _cfg_float(config, "scalping_stop_pct", 0.15) / 100.0
        min_edge = _cfg_float(config, "scalping_min_net_edge_bps", 30.0) / 10000.0
        slippage_round_trip = _cfg_float(config, "slippage_bps_per_side", 2.0) * 2 / 10000.0
        hold = timedelta(minutes=_cfg_int(config, "scalping_max_hold_minutes", 15))
        require_vwap = bool(config.get("intraday.scalping_require_vwap", True))
        if target_pct <= stop_pct + min_edge + slippage_round_trip:
            return []

        vwap_data = _with_vwap(data) if require_vwap else data

        for i in range(3, len(vwap_data)):
            ts = vwap_data.index[i].to_pydatetime()
            if ts < start or ts > end:
                continue
            prev = vwap_data.iloc[i - 3 : i]
            row = vwap_data.iloc[i]
            close = float(row["close"])
            avg_vol = _avg_volume(vwap_data.iloc[:i])
            if avg_vol > 0 and float(row["volume"]) < avg_vol * 1.5:
                continue
            vwap = (
                float(row["vwap"])
                if require_vwap and "vwap" in row.index and pd.notna(row["vwap"])
                else close
            )
            if close > float(prev["high"].max()):
                if require_vwap and close < vwap:
                    continue
                return [
                    IntradaySignal(
                        symbol,
                        self.name,
                        "BUY",
                        ts,
                        close,
                        close * (1.0 - stop_pct),
                        close * (1.0 + target_pct),
                        "Momentum scalp cleared recent highs above VWAP",
                        min(ts + hold, _force_exit_dt(ts, config)),
                    )
                ]
            if close < float(prev["low"].min()):
                if require_vwap and close > vwap:
                    continue
                return [
                    IntradaySignal(
                        symbol,
                        self.name,
                        "SELL",
                        ts,
                        close,
                        close * (1.0 + stop_pct),
                        close * (1.0 - target_pct),
                        "Momentum scalp cleared recent lows below VWAP",
                        min(ts + hold, _force_exit_dt(ts, config)),
                    )
                ]
        return []


def _trend_ok(frame: pd.DataFrame, config: Config, max_trend: float, as_of) -> bool:
    from src.intraday.ranking import IntradayRanker

    ts = pd.Timestamp(as_of) if as_of is not None else None
    return IntradayRanker.trend_cleanliness(frame, config, ts) <= max_trend


def _min_stop(entry: float, stop: float, side: str, config: Config) -> float:
    floor_pct = _cfg_float(config, "min_stop_pct", 0.20) / 100.0
    min_dist = entry * floor_pct
    if side == "BUY":
        return min(stop, entry - min_dist)
    return max(stop, entry + min_dist)


def _capped_vwap_target(
    entry: float,
    stop: float,
    vwap: float,
    side: str,
    config: Config,
) -> float:
    """Cap VWAP target by target_r_multiple to avoid chasing extended reverts."""
    target_r = _cfg_float(config, "target_r_multiple", 1.0)
    risk = abs(entry - stop)
    if side == "BUY":
        r_target = entry + risk * target_r
        if vwap > entry:
            return min(vwap, r_target)
        return entry
    r_target = entry - risk * target_r
    if vwap < entry:
        return max(vwap, r_target)
    return entry


def _signal_passes_cost_floor(
    config: Config,
    entry: float,
    target: float,
    stop: float,
    equity: float,
) -> bool:
    risk = abs(entry - stop)
    if risk <= 0:
        return False
    qty = estimate_signal_quantity(config, entry, risk, equity)
    return passes_min_net_edge(config, entry, target, max(qty, 1))


@_register
class VwapFadeStrategy(IntradayStrategy):
    name = "vwap_fade"

    def signals(
        self,
        symbol: str,
        frame: pd.DataFrame,
        config: Config,
        previous_day=None,
    ) -> List[IntradaySignal]:
        data = _with_vwap(_session(frame, config))
        sigma_lb = _cfg_int(config, "vwap_fade_sigma_lookback", 20)
        z_entry = _cfg_float(config, "vwap_fade_z_entry", 1.5)
        max_vol = _cfg_float(config, "vwap_fade_max_volume_ratio", 0.85)
        stop_buffer = _cfg_float(config, "stop_buffer_pct", 0.05) / 100.0
        sip = config.get("intraday.stocks_in_play", {}) or {}
        max_trend = float(sip.get("max_trend_cleanliness", 0.45) or 0.45)

        if len(data) < sigma_lb + 5:
            return []

        for i in range(sigma_lb, len(data)):
            window = data.iloc[: i + 1]
            row = data.iloc[i]
            ts = data.index[i].to_pydatetime()
            residuals = window["close"] - window["vwap"]
            sigma = float(residuals.tail(sigma_lb).std())
            if sigma <= 1e-9:
                continue
            z = float((row["close"] - row["vwap"]) / sigma)
            avg_vol = _avg_volume(window.iloc[:-1])
            vol_ratio = float(row["volume"]) / avg_vol if avg_vol > 0 else 1.0
            if not _trend_ok(window, config, max_trend, ts):
                continue
            session_low = float(window["low"].min())
            session_high = float(window["high"].max())
            close = float(row["close"])
            vwap = float(row["vwap"])

            if z <= -z_entry and vol_ratio < max_vol:
                stop = _min_stop(
                    close, session_low * (1.0 - stop_buffer), "BUY", config
                )
                if close <= stop or vwap <= close:
                    continue
                target = _capped_vwap_target(close, stop, vwap, "BUY", config)
                equity = float(config.secrets.initial_capital)
                if not _signal_passes_cost_floor(config, close, target, stop, equity):
                    continue
                return [
                    IntradaySignal(
                        symbol,
                        self.name,
                        "BUY",
                        ts,
                        close,
                        stop,
                        target,
                        f"VWAP fade long z={z:.2f}",
                        _force_exit_dt(ts, config),
                        entry_z=round(z, 4),
                    )
                ]
            if z >= z_entry and vol_ratio < max_vol:
                stop = _min_stop(
                    close, session_high * (1.0 + stop_buffer), "SELL", config
                )
                if close >= stop or vwap >= close:
                    continue
                target = _capped_vwap_target(close, stop, vwap, "SELL", config)
                equity = float(config.secrets.initial_capital)
                if not _signal_passes_cost_floor(config, close, target, stop, equity):
                    continue
                return [
                    IntradaySignal(
                        symbol,
                        self.name,
                        "SELL",
                        ts,
                        close,
                        stop,
                        target,
                        f"VWAP fade short z={z:.2f}",
                        _force_exit_dt(ts, config),
                        entry_z=round(z, 4),
                    )
                ]
        return []


@_register
class OpeningGapFadeStrategy(IntradayStrategy):
    name = "opening_gap_fade"

    def signals(
        self,
        symbol: str,
        frame: pd.DataFrame,
        config: Config,
        previous_day=None,
    ) -> List[IntradaySignal]:
        if not previous_day or not previous_day.get("close"):
            return []
        prev_close = float(previous_day["close"])
        day = frame.loc[frame.index.date == frame.index[0].date()]
        if day.empty:
            return []
        first = day.iloc[0]
        gap_pct = (float(first["open"]) / prev_close - 1.0) * 100.0
        if abs(gap_pct) > 1.0 or abs(gap_pct) < 0.3:
            return []

        entry_start = datetime.combine(day.index[0].date(), time(9, 45))
        stop_buffer = _cfg_float(config, "stop_buffer_pct", 0.05) / 100.0
        rest = day.loc[day.index >= pd.Timestamp(entry_start)]
        if rest.empty:
            return []

        avg_vol = float(day["volume"].mean())
        rvol = float(first["volume"]) / avg_vol if avg_vol > 0 else 0.0
        max_rvol = _cfg_float(config, "opening_gap_fade_max_rvol", 1.3)
        if rvol >= max_rvol:
            return []

        row = rest.iloc[0]
        ts = rest.index[0].to_pydatetime()
        close = float(row["close"])
        session_low = float(day["low"].min())
        session_high = float(day["high"].max())

        if gap_pct <= -0.3:
            stop = session_low * (1.0 - stop_buffer)
            target = prev_close
            equity = float(config.secrets.initial_capital)
            if not _signal_passes_cost_floor(config, close, target, stop, equity):
                return []
            return [
                IntradaySignal(
                    symbol,
                    self.name,
                    "BUY",
                    ts,
                    close,
                    stop,
                    prev_close,
                    f"Gap-down fade {gap_pct:+.2f}%",
                    _force_exit_dt(ts, config),
                )
            ]
        if gap_pct >= 0.3:
            stop = session_high * (1.0 + stop_buffer)
            target = prev_close
            equity = float(config.secrets.initial_capital)
            if not _signal_passes_cost_floor(config, close, target, stop, equity):
                return []
            return [
                IntradaySignal(
                    symbol,
                    self.name,
                    "SELL",
                    ts,
                    close,
                    stop,
                    prev_close,
                    f"Gap-up fade {gap_pct:+.2f}%",
                    _force_exit_dt(ts, config),
                )
            ]
        return []


@_register
class RangeRevertStrategy(IntradayStrategy):
    name = "range_revert"

    def signals(
        self,
        symbol: str,
        frame: pd.DataFrame,
        config: Config,
        previous_day=None,
    ) -> List[IntradaySignal]:
        day = frame.loc[frame.index.date == frame.index[0].date()]
        range_end = datetime.combine(day.index[0].date(), time(10, 15))
        range_window = day.loc[day.index <= pd.Timestamp(range_end)]
        trade_window = _session(day, config)
        if len(range_window) < 4 or len(trade_window) < 6:
            return []

        range_high = float(range_window["high"].max())
        range_low = float(range_window["low"].min())
        range_mid = (range_high + range_low) / 2.0
        atr = float((range_window["high"] - range_window["low"]).mean())
        if range_mid <= 0 or (range_high - range_low) < 0.5 * atr:
            return []

        after_range = trade_window.loc[trade_window.index > pd.Timestamp(range_end)]
        for i in range(1, len(after_range)):
            row = after_range.iloc[i]
            prev = after_range.iloc[i - 1]
            ts = after_range.index[i].to_pydatetime()
            close = float(row["close"])
            if close <= range_low + 0.1 * atr and close > float(prev["close"]):
                stop = range_low - 0.5 * atr
                return [
                    IntradaySignal(
                        symbol,
                        self.name,
                        "BUY",
                        ts,
                        close,
                        stop,
                        range_mid,
                        "Range lower band rejection",
                        _force_exit_dt(ts, config),
                    )
                ]
            if close >= range_high - 0.1 * atr and close < float(prev["close"]):
                stop = range_high + 0.5 * atr
                return [
                    IntradaySignal(
                        symbol,
                        self.name,
                        "SELL",
                        ts,
                        close,
                        stop,
                        range_mid,
                        "Range upper band rejection",
                        _force_exit_dt(ts, config),
                    )
                ]
        return []


@_register
class OfiMomentumStrategy(IntradayStrategy):
    name = "ofi_momentum"

    def signals(
        self,
        symbol: str,
        frame: pd.DataFrame,
        config: Config,
        previous_day=None,
    ) -> List[IntradaySignal]:
        data = _session(frame, config)
        if len(data) < 8:
            return []
        lookback = _cfg_int(config, "ofi_lookback_bars", 5)
        z_lookback = _cfg_int(config, "ofi_z_lookback", 60)
        z_entry = _cfg_float(config, "ofi_z_entry", 2.0)
        stop_pct = _cfg_float(config, "ofi_stop_pct", 0.20) / 100.0
        target_pct = _cfg_float(config, "ofi_target_pct", 0.35) / 100.0
        hold_min = _cfg_int(config, "ofi_max_hold_minutes", 15)

        for i in range(5, len(data)):
            window = data.iloc[: i + 1]
            row = data.iloc[i]
            ts = data.index[i].to_pydatetime()
            z = ofi_z_proxy(window, lookback, z_lookback)
            if ofi_divergence(window):
                continue
            prev = data.iloc[i - 3 : i]
            close = float(row["close"])
            prior_high = float(prev["high"].max())
            prior_low = float(prev["low"].min())
            hold_exit = min(
                ts + timedelta(minutes=hold_min),
                _force_exit_dt(ts, config),
            )
            if z >= z_entry and close > prior_high:
                stop = close * (1.0 - stop_pct)
                target = close * (1.0 + target_pct)
                equity = float(config.secrets.initial_capital)
                if not _signal_passes_cost_floor(config, close, target, stop, equity):
                    continue
                return [
                    IntradaySignal(
                        symbol,
                        self.name,
                        "BUY",
                        ts,
                        close,
                        close * (1.0 - stop_pct),
                        close * (1.0 + target_pct),
                        f"OFI momentum long z={z:.2f}",
                        hold_exit,
                    )
                ]
            if z <= -z_entry and close < prior_low:
                stop = close * (1.0 + stop_pct)
                target = close * (1.0 - target_pct)
                equity = float(config.secrets.initial_capital)
                if not _signal_passes_cost_floor(config, close, target, stop, equity):
                    continue
                return [
                    IntradaySignal(
                        symbol,
                        self.name,
                        "SELL",
                        ts,
                        close,
                        close * (1.0 + stop_pct),
                        close * (1.0 - target_pct),
                        f"OFI momentum short z={z:.2f}",
                        hold_exit,
                    )
                ]
        return []


@_register
class OfiExhaustionStrategy(IntradayStrategy):
    name = "ofi_exhaustion"

    def signals(
        self,
        symbol: str,
        frame: pd.DataFrame,
        config: Config,
        previous_day=None,
    ) -> List[IntradaySignal]:
        data = _session(frame, config)
        if len(data) < 8:
            return []
        lookback = _cfg_int(config, "ofi_lookback_bars", 5)
        z_lookback = _cfg_int(config, "ofi_z_lookback", 60)
        z_entry = _cfg_float(config, "ofi_exhaustion_z", 2.5)
        target_pct = _cfg_float(config, "ofi_target_pct", 0.35) / 100.0
        stop_buffer = _cfg_float(config, "stop_buffer_pct", 0.05) / 100.0
        hold_min = _cfg_int(config, "ofi_max_hold_minutes", 15)

        for i in range(5, len(data)):
            window = data.iloc[: i + 1]
            row = data.iloc[i]
            ts = data.index[i].to_pydatetime()
            z = ofi_z_proxy(window, lookback, z_lookback)
            if not ofi_divergence(window):
                continue
            close = float(row["close"])
            high = float(row["high"])
            low = float(row["low"])
            hold_exit = min(ts + timedelta(minutes=hold_min), _force_exit_dt(ts, config))
            if z >= z_entry and close < high - 0.3 * (high - low):
                stop = high * (1.0 + stop_buffer)
                target = close * (1.0 - target_pct)
                equity = float(config.secrets.initial_capital)
                if not _signal_passes_cost_floor(config, close, target, stop, equity):
                    continue
                return [
                    IntradaySignal(
                        symbol,
                        self.name,
                        "SELL",
                        ts,
                        close,
                        high * (1.0 + stop_buffer),
                        close * (1.0 - target_pct),
                        f"OFI exhaustion fade z={z:.2f}",
                        hold_exit,
                    )
                ]
            if z <= -z_entry and close > low + 0.3 * (high - low):
                stop = low * (1.0 - stop_buffer)
                target = close * (1.0 + target_pct)
                equity = float(config.secrets.initial_capital)
                if not _signal_passes_cost_floor(config, close, target, stop, equity):
                    continue
                return [
                    IntradaySignal(
                        symbol,
                        self.name,
                        "BUY",
                        ts,
                        close,
                        low * (1.0 - stop_buffer),
                        close * (1.0 + target_pct),
                        f"OFI exhaustion fade z={z:.2f}",
                        hold_exit,
                    )
                ]
        return []


@_register
class PairZscoreStrategy(IntradayStrategy):
    name = "pair_zscore"

    def signals(
        self,
        symbol: str,
        frame: pd.DataFrame,
        config: Config,
        previous_day=None,
    ) -> List[IntradaySignal]:
        return scan_pair_signals(config, {symbol: frame}).get(symbol, [])


@_register
class TwapSliceStrategy(IntradayStrategy):
    name = "twap_slice"

    def signals(
        self,
        symbol: str,
        frame: pd.DataFrame,
        config: Config,
        previous_day=None,
    ) -> List[IntradaySignal]:
        return _schedule_signals(symbol, frame, config, "twap")


@_register
class VwapScheduleStrategy(IntradayStrategy):
    name = "vwap_schedule"

    def signals(
        self,
        symbol: str,
        frame: pd.DataFrame,
        config: Config,
        previous_day=None,
    ) -> List[IntradaySignal]:
        return _schedule_signals(symbol, frame, config, "vwap")


def scan_pair_signals(
    config: Config,
    frames: Dict[str, pd.DataFrame],
) -> Dict[str, List[IntradaySignal]]:
    """Return primary-leg pair signals keyed by leg_a symbol."""
    entry_z = _cfg_float(config, "pair_entry_z", 1.5)
    min_bars = _cfg_int(config, "pair_min_bars", 20)
    max_widen = _cfg_float(config, "pair_max_widen_z", 0.4)
    stop_buffer = _cfg_float(config, "pair_stop_buffer_pct", 0.30) / 100.0
    window_start = _parse_time(str(config.get("intraday.trade_window_start", "09:45")))
    window_end = _parse_time(str(config.get("intraday.trade_window_end", "14:30")))
    equity = float(config.secrets.initial_capital)
    out: Dict[str, List[IntradaySignal]] = {}

    for leg_a, leg_b in configured_pairs(config):
        fa = frames.get(leg_a)
        fb = frames.get(leg_b)
        if fa is None or fb is None or fa.empty or fb.empty:
            continue
        z_hist = spread_z_history(fa, fb, min_bars=min_bars)
        if z_hist.empty:
            continue
        beta = pair_beta(fa, fb)

        for ts, z_now in z_hist.items():
            bar_time = ts.time() if hasattr(ts, "time") else ts.to_pydatetime().time()
            if bar_time < window_start or bar_time > window_end:
                continue
            if abs(z_now) < entry_z:
                continue
            idx = z_hist.index.get_loc(ts)
            if idx >= 3 and abs(z_now - float(z_hist.iloc[idx - 3])) > max_widen:
                continue

            close_a = float(fa.loc[ts, "close"])
            close_b = float(fb.loc[ts, "close"])
            pair_id = f"{leg_a}:{leg_b}"
            force_exit = _force_exit_dt(ts.to_pydatetime(), config)
            ts_dt = ts.to_pydatetime()

            if z_now >= entry_z:
                stop_a = close_a * (1.0 + stop_buffer)
                target_a = close_a * (1.0 - stop_buffer * 2)
                stop_b = close_b * (1.0 - stop_buffer)
                target_b = close_b * (1.0 + stop_buffer * 2)
                signal = IntradaySignal(
                    leg_a,
                    "pair_zscore",
                    "SELL",
                    ts_dt,
                    close_a,
                    stop_a,
                    target_a,
                    f"Short spread z={z_now:.2f}",
                    force_exit,
                    pair_id=pair_id,
                    pair_leg=leg_b,
                    pair_side="BUY",
                    pair_entry_price=close_b,
                    pair_stop_price=stop_b,
                    pair_target_price=target_b,
                    entry_z=round(float(z_now), 4),
                    pair_beta=round(beta, 4),
                    use_trailing_stop=False,
                )
            else:
                stop_a = close_a * (1.0 - stop_buffer)
                target_a = close_a * (1.0 + stop_buffer * 2)
                stop_b = close_b * (1.0 + stop_buffer)
                target_b = close_b * (1.0 - stop_buffer * 2)
                signal = IntradaySignal(
                    leg_a,
                    "pair_zscore",
                    "BUY",
                    ts_dt,
                    close_a,
                    stop_a,
                    target_a,
                    f"Long spread z={z_now:.2f}",
                    force_exit,
                    pair_id=pair_id,
                    pair_leg=leg_b,
                    pair_side="SELL",
                    pair_entry_price=close_b,
                    pair_stop_price=stop_b,
                    pair_target_price=target_b,
                    entry_z=round(float(z_now), 4),
                    pair_beta=round(beta, 4),
                    use_trailing_stop=False,
                )
            if not _signal_passes_cost_floor(
                config, close_a, target_a, stop_a, equity
            ) or not _signal_passes_cost_floor(
                config, close_b, target_b, stop_b, equity
            ):
                continue
            out.setdefault(leg_a, []).append(signal)
            break
    return out


def scan_cross_signals(
    config: Config,
    frames: Dict[str, pd.DataFrame],
) -> Dict[str, List[IntradaySignal]]:
    """Return equity-leg cross signals keyed by leg symbol."""
    entry_z = _cfg_float(config, "cross_entry_z", 2.0)
    min_bars = _cfg_int(config, "cross_min_bars", 25)
    max_widen = _cfg_float(config, "cross_max_widen_z", 0.35)
    stop_buffer = _cfg_float(config, "cross_stop_buffer_pct", 0.30) / 100.0
    beta_lookback = _cfg_int(config, "cross_beta_lookback", 60)
    window_start = _parse_time(str(config.get("intraday.trade_window_start", "09:45")))
    window_end = _parse_time(str(config.get("intraday.trade_window_end", "14:30")))
    equity = float(config.secrets.initial_capital)
    out: Dict[str, List[IntradaySignal]] = {}

    for basket in configured_cross_baskets(config):
        anchor = basket.anchor
        leg = basket.leg
        fa = frames.get(anchor)
        fb = frames.get(leg)
        if fa is None or fb is None or fa.empty or fb.empty:
            continue
        beta = basket.beta if basket.beta is not None else hedge_beta(fa, fb, beta_lookback)
        z_hist = residual_z_history(
            fa, fb, beta=beta, beta_lookback=beta_lookback, min_bars=min_bars
        )
        if z_hist.empty:
            continue

        for ts, z_now in z_hist.items():
            bar_time = ts.time() if hasattr(ts, "time") else ts.to_pydatetime().time()
            if bar_time < window_start or bar_time > window_end:
                continue
            if abs(z_now) < entry_z:
                continue
            idx = z_hist.index.get_loc(ts)
            if idx >= 3 and abs(z_now - float(z_hist.iloc[idx - 3])) > max_widen:
                continue

            close_anchor = float(fa.loc[ts, "close"])
            close_leg = float(fb.loc[ts, "close"])
            pair_id = basket.basket_id
            force_exit = _force_exit_dt(ts.to_pydatetime(), config)
            ts_dt = ts.to_pydatetime()

            if z_now >= entry_z:
                stop_leg = close_leg * (1.0 + stop_buffer)
                target_leg = close_leg * (1.0 - stop_buffer * 2)
                stop_anchor = close_anchor * (1.0 - stop_buffer)
                target_anchor = close_anchor * (1.0 + stop_buffer * 2)
                signal = IntradaySignal(
                    leg,
                    "cross_zscore",
                    "SELL",
                    ts_dt,
                    close_leg,
                    stop_leg,
                    target_leg,
                    f"Short residual z={z_now:.2f}",
                    force_exit,
                    pair_id=pair_id,
                    pair_leg=anchor,
                    pair_side="BUY",
                    pair_entry_price=close_anchor,
                    pair_stop_price=stop_anchor,
                    pair_target_price=target_anchor,
                    entry_z=round(float(z_now), 4),
                    pair_beta=round(beta, 4),
                    use_trailing_stop=False,
                )
            else:
                stop_leg = close_leg * (1.0 - stop_buffer)
                target_leg = close_leg * (1.0 + stop_buffer * 2)
                stop_anchor = close_anchor * (1.0 + stop_buffer)
                target_anchor = close_anchor * (1.0 - stop_buffer * 2)
                signal = IntradaySignal(
                    leg,
                    "cross_zscore",
                    "BUY",
                    ts_dt,
                    close_leg,
                    stop_leg,
                    target_leg,
                    f"Long residual z={z_now:.2f}",
                    force_exit,
                    pair_id=pair_id,
                    pair_leg=anchor,
                    pair_side="SELL",
                    pair_entry_price=close_anchor,
                    pair_stop_price=stop_anchor,
                    pair_target_price=target_anchor,
                    entry_z=round(float(z_now), 4),
                    pair_beta=round(beta, 4),
                    use_trailing_stop=False,
                )
            if not _signal_passes_cost_floor(
                config, close_leg, target_leg, stop_leg, equity
            ) or not _signal_passes_cost_floor(
                config, close_anchor, target_anchor, stop_anchor, equity
            ):
                continue
            out.setdefault(leg, []).append(signal)
            break
    return out


@_register
class CrossZscoreStrategy(IntradayStrategy):
    name = "cross_zscore"

    def signals(
        self,
        symbol: str,
        frame: pd.DataFrame,
        config: Config,
        previous_day=None,
    ) -> List[IntradaySignal]:
        return scan_cross_signals(config, {symbol: frame}).get(symbol, [])


def _schedule_signals(
    symbol: str,
    frame: pd.DataFrame,
    config: Config,
    schedule_type: str,
) -> List[IntradaySignal]:
    orders = config.get("intraday.parent_orders") or []
    order = next(
        (o for o in orders if isinstance(o, dict) and str(o.get("symbol", "")).upper() == symbol),
        None,
    )
    if not order:
        return []

    start = _parse_time(str(order.get("start", "10:00")))
    end = _parse_time(str(order.get("end", "14:00")))
    side = str(order.get("side", "BUY")).upper()
    qty = int(order.get("qty", 0) or 0)
    if qty < 1:
        return []

    day = frame.loc[frame.index.date == frame.index[0].date()]
    window = day.between_time(start, end)
    if window.empty:
        return []

    slices = max(int(order.get("slices", 8) or 8), 1)
    stride = max(len(window) // slices, 1)
    signals: List[IntradaySignal] = []
    for i in range(0, len(window), stride):
        if len(signals) >= slices:
            break
        row = window.iloc[i]
        ts = window.index[i].to_pydatetime()
        close = float(row["close"])
        stop = close * (0.995 if side == "BUY" else 1.005)
        target = close * (1.005 if side == "BUY" else 0.995)
        signals.append(
            IntradaySignal(
                symbol,
                "twap_slice" if schedule_type == "twap" else "vwap_schedule",
                side,
                ts,
                close,
                stop,
                target,
                f"{schedule_type} slice {len(signals) + 1}/{slices}",
                datetime.combine(ts.date(), end),
            )
        )
    return signals[:1]


def load_strategies(config: Config) -> List[IntradayStrategy]:
    """Return enabled strategies from config (default: ORB + VWAP only)."""
    allowed = config.get("intraday.strategies")
    if not allowed:
        allowed = ["opening_range_breakout", "vwap_pullback"]
    out: List[IntradayStrategy] = []
    for name in allowed:
        cls = STRATEGY_REGISTRY.get(str(name))
        if cls is not None:
            out.append(cls())
    return out
