"""Rank stocks by how trader-friendly they look intraday."""

from __future__ import annotations

from typing import Dict, List, Optional

import pandas as pd

from src.config import Config
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
        min_turnover = float(self._config.get("intraday.min_avg_turnover_inr", 1e8) or 1e8)
        min_range = float(self._config.get("intraday.min_atr_pct", 0.6) or 0.6)

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
        trend_cleanliness = self._trend_cleanliness(frame)

        if avg_volume < min_volume or turnover < min_turnover or range_pct < min_range:
            return None
        if trend_cleanliness < 0.25:
            return None

        turnover_score = min(turnover / min_turnover, 4.0) * 18.0
        volume_score = min(volume_expansion, 4.0) * 12.0
        range_score = min(range_pct / max(min_range, 0.01), 4.0) * 13.0
        gap_score = min(abs(gap_pct or 0.0), 3.0) * 5.0
        vwap_score = min(abs(vwap_distance_pct or 0.0), 1.5) * 8.0
        level_score = max(0.0, 2.0 - (prior_distance if prior_distance is not None else 2.0)) * 8.0
        trend_score = trend_cleanliness * 22.0
        score = turnover_score + volume_score + range_score + gap_score + vwap_score + level_score + trend_score

        reasons = [
            f"turnover Rs {turnover:,.0f}",
            f"volume expansion {volume_expansion:.2f}x",
            f"range {range_pct:.2f}%",
            f"trend cleanliness {trend_cleanliness:.2f}",
        ]
        if gap_pct is not None:
            reasons.append(f"gap {gap_pct:+.2f}%")
        if prior_distance is not None:
            reasons.append(f"prior level distance {prior_distance:.2f}%")

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
            reasons=reasons,
        )

    @staticmethod
    def _vwap(frame: pd.DataFrame) -> Optional[float]:
        volume = frame["volume"].clip(lower=0)
        total_volume = float(volume.sum())
        if total_volume <= 0:
            return None
        typical = (frame["high"] + frame["low"] + frame["close"]) / 3.0
        return float((typical * volume).sum() / total_volume)

    @staticmethod
    def _prior_level_distance(close: float, previous_day: Optional[Dict[str, float]]) -> Optional[float]:
        if not previous_day:
            return None
        levels = [previous_day.get("high"), previous_day.get("low")]
        levels = [float(v) for v in levels if v]
        if not levels or close <= 0:
            return None
        return min(abs(close / level - 1.0) * 100.0 for level in levels if level > 0)

    @staticmethod
    def _trend_cleanliness(frame: pd.DataFrame) -> float:
        closes = frame["close"].astype(float)
        if len(closes) < 4:
            return 0.0
        diffs = closes.diff().dropna()
        up = int((diffs > 0).sum())
        down = int((diffs < 0).sum())
        directional = abs(up - down) / max(len(diffs), 1)
        crosses = int((diffs.apply(lambda x: 1 if x > 0 else -1 if x < 0 else 0).diff().abs() > 0).sum())
        chop_penalty = min(crosses / max(len(diffs), 1), 1.0)
        return max(0.0, min(1.0, directional * 0.75 + (1.0 - chop_penalty) * 0.25))
