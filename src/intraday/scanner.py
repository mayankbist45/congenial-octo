"""Intraday stock scanner that maps candidates to playbooks."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

import pandas as pd

from src.config import Config
from src.intraday.strategies import DEFAULT_STRATEGIES


@dataclass
class IntradayCandidate:
    symbol: str
    strategies: List[str] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)
    turnover: float = 0.0
    atr_pct: float = 0.0


class IntradayScanner:
    def __init__(self, config: Config):
        self._config = config

    def scan(
        self,
        frames: Dict[str, pd.DataFrame],
        previous_days: Optional[Dict[str, Dict[str, float]]] = None,
    ) -> List[IntradayCandidate]:
        previous_days = previous_days or {}
        out: List[IntradayCandidate] = []
        for symbol, frame in frames.items():
            candidate = self._candidate(symbol, frame, previous_days.get(symbol))
            if candidate and candidate.strategies:
                out.append(candidate)
        out.sort(key=lambda c: (len(c.strategies), c.turnover, c.atr_pct), reverse=True)
        return out

    def _candidate(
        self,
        symbol: str,
        frame: pd.DataFrame,
        previous_day: Optional[Dict[str, float]],
    ) -> Optional[IntradayCandidate]:
        if frame.empty or len(frame) < 6:
            return None
        min_volume = float(self._config.get("intraday.min_avg_volume", 100000) or 100000)
        min_turnover = float(self._config.get("intraday.min_avg_turnover_inr", 1e8) or 1e8)
        min_atr_pct = float(self._config.get("intraday.min_atr_pct", 0.6) or 0.6)

        avg_volume = float(frame["volume"].mean())
        avg_price = float(frame["close"].mean())
        turnover = float(frame["volume"].sum()) * avg_price
        atr_pct = self._intraday_range_pct(frame)
        if avg_volume < min_volume or turnover < min_turnover or atr_pct < min_atr_pct:
            return None

        strategies: List[str] = []
        reasons = [f"turnover Rs {turnover:,.0f}", f"intraday range {atr_pct:.2f}%"]
        for strategy in DEFAULT_STRATEGIES:
            if strategy.name == "previous_day_break" and not previous_day:
                continue
            if strategy.signals(symbol, frame, self._config, previous_day):
                strategies.append(strategy.name)
        return IntradayCandidate(symbol=symbol, strategies=strategies, reasons=reasons, turnover=turnover, atr_pct=atr_pct)

    @staticmethod
    def _intraday_range_pct(frame: pd.DataFrame) -> float:
        close = float(frame["close"].iloc[0])
        if close <= 0:
            return 0.0
        return (float(frame["high"].max()) / float(frame["low"].min()) - 1.0) * 100.0
