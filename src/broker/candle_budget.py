"""Global spacing and per-minute cap for Angel One candle fetches."""

from __future__ import annotations

import time
from collections import deque
from datetime import datetime
from typing import Deque, List, Optional

from src.broker.base import BrokerClient
from src.config import Config, get_config
from src.logging_setup import get_logger

logger = get_logger(__name__)


class CandleBudgetCoordinator(BrokerClient):
    """Wrap a broker and enforce shared request spacing across all API keys."""

    def __init__(self, inner: BrokerClient, config: Optional[Config] = None):
        self._inner = inner
        self._config = config or get_config()
        self._last_call_at = 0.0
        self._recent_calls: Deque[float] = deque()
        self._throttle_count = 0
        self._success_count = 0

    @property
    def name(self) -> str:
        return f"CandleBudget({self._inner.name})"

    @property
    def inner(self) -> BrokerClient:
        return self._inner

    def _spacing_seconds(self) -> float:
        return float(
            self._config.get("angelone.candle_budget_spacing_seconds", 0)
            or self._config.get("angelone.candle_call_interval_seconds", 4.0)
            or 4.0
        )

    def _max_per_minute(self) -> int:
        return int(self._config.get("angelone.candle_max_requests_per_minute", 12) or 12)

    def _wait_for_budget(self) -> None:
        spacing = self._spacing_seconds()
        now = time.time()
        elapsed = now - self._last_call_at
        if elapsed < spacing:
            time.sleep(spacing - elapsed)
            now = time.time()

        window_start = now - 60.0
        while self._recent_calls and self._recent_calls[0] < window_start:
            self._recent_calls.popleft()
        cap = self._max_per_minute()
        if cap > 0 and len(self._recent_calls) >= cap:
            sleep_for = self._recent_calls[0] + 60.0 - now
            if sleep_for > 0:
                logger.debug(
                    "Candle budget: at %d req/min cap, sleeping %.1fs",
                    cap,
                    sleep_for,
                )
                time.sleep(sleep_for)
            now = time.time()
            while self._recent_calls and self._recent_calls[0] < now - 60.0:
                self._recent_calls.popleft()

        self._last_call_at = time.time()
        self._recent_calls.append(self._last_call_at)

    def connect(self) -> bool:
        return self._inner.connect()

    def disconnect(self) -> None:
        self._inner.disconnect()

    def get_holdings(self):
        return self._inner.get_holdings()

    def get_funds(self):
        return self._inner.get_funds()

    def get_last_price(self, symbol: str):
        return self._inner.get_last_price(symbol)

    def get_last_prices(self, symbols: List[str]):
        return self._inner.get_last_prices(symbols)

    def place_order(self, request):
        return self._inner.place_order(request)

    def get_candles(
        self,
        symbol: str,
        from_date: datetime,
        to_date: datetime,
        interval: str = "ONE_DAY",
    ):
        self._wait_for_budget()
        rows = self._inner.get_candles(symbol, from_date, to_date, interval)
        if rows:
            self._success_count += 1
        return rows

    def budget_stats(self) -> dict:
        return {
            "success": self._success_count,
            "throttles": self._throttle_count,
            "recent_per_minute": len(self._recent_calls),
        }
