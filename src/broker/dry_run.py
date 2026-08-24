"""Dry-run wrapper.

Wraps a live broker so writes are intercepted while quotes remain live. Callers
may provide a paper-book broker when simulated holdings and cash must be kept
separate from the authenticated broker account.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from src.broker.base import (
    BrokerClient,
    BrokerFunds,
    BrokerHolding,
    OrderRequest,
    OrderResult,
)
from src.logging_setup import get_logger

logger = get_logger(__name__)


class DryRunBroker(BrokerClient):
    def __init__(
        self,
        inner: BrokerClient,
        *,
        paper_book: Optional[BrokerClient] = None,
    ):
        self._inner = inner
        self._paper_book = paper_book

    @property
    def name(self) -> str:
        return f"{self._inner.name} (dry run)"

    @property
    def inner(self) -> BrokerClient:
        return self._inner

    def connect(self) -> bool:
        return self._inner.connect()

    def disconnect(self) -> None:
        self._inner.disconnect()

    def get_holdings(self) -> List[BrokerHolding]:
        if self._paper_book is not None:
            return self._paper_book.get_holdings()
        return self._inner.get_holdings()

    def get_funds(self) -> BrokerFunds:
        if self._paper_book is not None:
            return self._paper_book.get_funds()
        return self._inner.get_funds()

    def get_last_price(self, symbol: str) -> Optional[float]:
        return self._inner.get_last_price(symbol)

    def get_last_prices(self, symbols: List[str]) -> Dict[str, float]:
        return self._inner.get_last_prices(symbols)

    def get_candles(self, *args, **kwargs):
        getter = getattr(self._inner, "get_candles", None)
        if getter is None:
            return []
        return getter(*args, **kwargs)

    def place_order(self, request: OrderRequest) -> OrderResult:
        logger.warning(
            "[DRY RUN] Suppressed %s %s x%d @ %s",
            request.side,
            request.symbol,
            request.quantity,
            request.price,
        )
        return OrderResult(
            success=True,
            symbol=request.symbol,
            side=request.side,
            quantity=request.quantity,
            order_id=f"DRYRUN-{request.symbol}-{request.side}",
            price=request.price,
            status="dry_run",
            dry_run=True,
        )
