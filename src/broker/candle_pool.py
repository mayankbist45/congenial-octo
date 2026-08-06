"""Route Angel One getCandleData across multiple API keys.

Uses primary-first failover: try the primary key until rate-limited, then
secondary. Fail-fast on throttle (no 45s sleep) so the pool can hand off quickly.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Dict, List, Optional

from src.broker.angelone import AngelOneClient
from src.broker.base import BrokerError, BrokerClient
from src.config import Config, get_config
from src.logging_setup import get_logger

logger = get_logger(__name__)


class AngelOneCandlePool(BrokerClient):
    """Market-data broker that spreads ``getCandleData`` across Angel One clients."""

    def __init__(self, clients: List[AngelOneClient], config: Optional[Config] = None):
        if len(clients) < 2:
            raise ValueError("AngelOneCandlePool requires at least two clients")
        self._config = config or get_config()
        self._clients = clients
        self._cooldown_until: Dict[str, float] = {}
        self._backoff = float(
            self._config.get("angelone.rate_limit_backoff_seconds", 60.0) or 60.0
        )

    @property
    def name(self) -> str:
        labels = [c.credential_label for c in self._clients]
        return f"AngelOneCandlePool({'+'.join(labels)})"

    @property
    def primary(self) -> AngelOneClient:
        return self._clients[0]

    def connect(self) -> bool:
        ok = True
        for client in self._clients:
            try:
                ok = client.connect() and ok
            except BrokerError as exc:
                logger.warning(
                    "Angel One [%s] connect failed: %s", client.credential_label, exc
                )
                ok = False
        return ok

    def disconnect(self) -> None:
        for client in self._clients:
            client.disconnect()

    def get_holdings(self):
        return self.primary.get_holdings()

    def get_funds(self):
        return self.primary.get_funds()

    def get_last_price(self, symbol: str):
        return self.primary.get_last_price(symbol)

    def get_last_prices(self, symbols: List[str]):
        return self.primary.get_last_prices(symbols)

    def place_order(self, request):
        return self.primary.place_order(request)

    def get_candles(
        self,
        symbol: str,
        from_date: datetime,
        to_date: datetime,
        interval: str = "ONE_DAY",
    ):
        errors: List[str] = []
        now = time.time()

        for idx, client in enumerate(self._clients):
            label = client.credential_label
            if now < self._cooldown_until.get(label, 0.0):
                logger.debug(
                    "Skipping Angel One [%s] for candles (cooldown %.0fs left)",
                    label,
                    self._cooldown_until[label] - now,
                )
                continue
            try:
                rows = client.get_candles(symbol, from_date, to_date, interval, strict=True)
            except BrokerError as exc:
                message = str(exc)
                errors.append(f"[{label}] {message}")
                if client.was_rate_limited(message):
                    self._cooldown_until[label] = time.time() + self._backoff
                    logger.warning(
                        "Angel One [%s] rate limited on getCandleData; "
                        "trying next API key (cooldown %.0fs)",
                        label,
                        self._backoff,
                    )
                    continue
                logger.warning(
                    "Angel One [%s] candle fetch failed for %s: %s",
                    label,
                    symbol,
                    message,
                )
                continue

            if idx > 0:
                logger.info(
                    "Candles for %s served by Angel One [%s] after failover",
                    symbol,
                    label,
                )
            return rows

        detail = "; ".join(errors) if errors else "all API keys in cooldown"
        logger.warning("All Angel One keys failed for %s candles: %s", symbol, detail)
        return []
