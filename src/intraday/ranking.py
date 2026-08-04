"""Rank intraday names; Stocks-in-Play filter (Zarattini et al. 2024)."""

from __future__ import annotations

from datetime import time
from typing import Dict, List, Optional

import pandas as pd

from src.config import Config
from src.market.session import session_end_time
from src.intraday.models import RankedStock


class IntradayRanker:
    def __init__(self, config: Config):
        self._config = config

    def rank(
        self,
        frames: Dict[str, pd.DataFrame],
        previous_days: Optional[Dict[str, Dict[str, float]]] = None,
    ) -> List[RankedStock]:
        previous_days = previous_days or {}
        ranked: List[RankedStock] = []
        for symbol, frame in frames.items():
            item = self._score(symbol, frame, previous_days.get(symbol))
            if item:
                ranked.append(item)
        ranked.sort(key=lambda r: r.score, reverse=True)
        return ranked

    def _score(
        self,
        symbol: str,
        frame: pd.DataFrame,
        previous_day: Optional[Dict[str, float]],
    ) -> Optional[RankedStock]:
        if frame.empty or len(frame) < 6:
            return None

        min_volume = float(self._config.get("intraday.min_avg_volume", 100000) or 100000)
        min_turnover = float(
            self._config.get("intraday.min_avg_turnover_inr", 1e8) or 1e8
        )
        min_range = float(self._config.get("intraday.min_atr_pct", 0.6) or 0.6)
        sip = self._config.get("intraday.stocks_in_play", {}) or {}
        min_gap = float(sip.get("min_gap_pct", 0.8) or 0.8)
        min_rvol = float(sip.get("min_rvol", 2.0) or 2.0)
        min_trend = float(sip.get("min_trend_cleanliness", 0.5) or 0.5)

        close = float(frame["close"].iloc[-1])
        first = float(frame["open"].iloc[0])
        turnover = float((frame["close"] * frame["volume"]).sum())
        avg_volume = float(frame["volume"].mean())
        recent_volume = float(frame["volume"].tail(3).mean())
        volume_expansion = recent_volume / avg_volume if avg_volume > 0 else 0.0
        range_pct = (float(frame["high"].max()) / float(frame["low"].min()) - 1.0) * 100.0
        gap_pct = None
        if previous_day and previous_day.get("close"):
            gap_pct = (first / float(previous_day["close"]) - 1.0) * 100.0
        vwap = self._vwap(frame)
        vwap_distance_pct = (close / vwap - 1.0) * 100.0 if vwap else None
        prior_distance = self._prior_level_distance(close, previous_day)
        trend_cleanliness = self.trend_cleanliness(frame, self._config)

        if avg_volume < min_volume or turnover < min_turnover or range_pct < min_range:
            return None

        in_play = self._is_in_play(gap_pct, volume_expansion, turnover, min_turnover, min_gap, min_rvol)
        # Volume-in-play names can pass even when the open was choppy; the trade gate
        # checks trend again at signal time (post 09:45, up to the entry bar).
        if trend_cleanliness < min_trend and not in_play:
            return None

        turnover_score = min(turnover / min_turnover, 4.0) * 18.0
        volume_score = min(volume_expansion, 4.0) * 12.0
        range_score = min(range_pct / max(min_range, 0.01), 4.0) * 13.0
        gap_score = min(abs(gap_pct or 0.0), 3.0) * 5.0
        vwap_score = min(abs(vwap_distance_pct or 0.0), 1.5) * 8.0
        level_score = max(0.0, 2.0 - (prior_distance if prior_distance is not None else 2.0)) * 8.0
        trend_score = trend_cleanliness * 22.0
        play_bonus = 15.0 if in_play else -10.0
        score = (
            turnover_score
            + volume_score
            + range_score
            + gap_score
            + vwap_score
            + level_score
            + trend_score
            + play_bonus
        )

        reasons = [
            f"turnover Rs {turnover:,.0f}",
            f"volume expansion {volume_expansion:.2f}x",
            f"range {range_pct:.2f}%",
            f"trend cleanliness {trend_cleanliness:.2f}",
            f"in_play={in_play}",
        ]
        if gap_pct is not None:
            reasons.append(f"gap {gap_pct:+.2f}%")

        return RankedStock(
            symbol=symbol,
            score=round(score, 3),
            turnover=turnover,
            volume_expansion=volume_expansion,
            range_pct=range_pct,
            gap_pct=gap_pct,
            vwap_distance_pct=vwap_distance_pct,
            prior_level_distance_pct=prior_distance,
            trend_cleanliness=trend_cleanliness,
            in_play=in_play,
            reasons=reasons,
        )

    @staticmethod
    def _is_in_play(
        gap_pct: Optional[float],
        volume_expansion: float,
        turnover: float,
        min_turnover: float,
        min_gap: float,
        min_rvol: float,
    ) -> bool:
        gap_ok = gap_pct is not None and abs(gap_pct) >= min_gap
        rvol_ok = volume_expansion >= min_rvol
        turnover_ok = turnover >= min_turnover * 1.5
        return gap_ok or rvol_ok or turnover_ok

    @staticmethod
    def _vwap(frame: pd.DataFrame) -> Optional[float]:
        volume = frame["volume"].clip(lower=0)
        total_volume = float(volume.sum())
        if total_volume <= 0:
            return None
        typical = (frame["high"] + frame["low"] + frame["close"]) / 3.0
        return float((typical * volume).sum() / total_volume)

    @staticmethod
    def _prior_level_distance(
        close: float, previous_day: Optional[Dict[str, float]]
    ) -> Optional[float]:
        if not previous_day:
            return None
        levels = [previous_day.get("high"), previous_day.get("low")]
        levels = [float(v) for v in levels if v]
        if not levels or close <= 0:
            return None
        return min(abs(close / level - 1.0) * 100.0 for level in levels if level > 0)

    @classmethod
    def trend_cleanliness(
        cls,
        frame: pd.DataFrame,
        config: Optional[Config] = None,
        as_of: Optional[pd.Timestamp] = None,
    ) -> float:
        """Directional trend score on the tradable session, not the opening auction chop."""
        data = cls._trend_frame(frame, config, as_of)
        return cls._trend_cleanliness(data)

    @staticmethod
    def _parse_session_start(config: Optional[Config]) -> time:
        if config is None:
            return time(9, 45)
        raw = str(config.get("intraday.trade_window_start", "09:45"))
        hour, minute = raw.split(":", 1)
        return time(int(hour), int(minute))

    @classmethod
    def _trend_frame(
        cls,
        frame: pd.DataFrame,
        config: Optional[Config] = None,
        as_of: Optional[pd.Timestamp] = None,
    ) -> pd.DataFrame:
        if frame.empty:
            return frame
        start = cls._parse_session_start(config)
        end = session_end_time(config) if config is not None else time(15, 30)
        data = frame.between_time(start, end)
        if data.empty:
            data = frame
        if as_of is not None:
            data = data.loc[data.index <= pd.Timestamp(as_of)]
        return data

    @staticmethod
    def _trend_cleanliness(frame: pd.DataFrame) -> float:
        closes = frame["close"].astype(float)
        if len(closes) < 4:
            return 0.0
        diffs = closes.diff().dropna()
        up = int((diffs > 0).sum())
        down = int((diffs < 0).sum())
        directional = abs(up - down) / max(len(diffs), 1)
        signs = diffs.apply(lambda x: 1 if x > 0 else -1 if x < 0 else 0)
        crosses = int(signs.diff().abs().gt(0).sum())
        chop_penalty = min(crosses / max(len(diffs), 1), 1.0)
        return max(0.0, min(1.0, directional * 0.75 + (1.0 - chop_penalty) * 0.25))
