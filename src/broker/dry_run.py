"""Dry-run wrapper.

Wraps a live broker so reads (holdings, funds, quotes) hit the real account
while writes (orders) are intercepted. This is the safest configuration for
watching the fund make real decisions against a real portfolio before letting
it trade.
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
    def __init__(self, inner: BrokerClient):
        self._inner = inner

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
        return self._inner.get_holdings()

    def get_funds(self) -> BrokerFunds:
        return self._inner.get_funds()

    def get_last_price(self, symbol: str) -> Optional[float]:
        return self._inner.get_last_price(symbol)

    def get_last_prices(self, symbols: List[str]) -> Dict[str, float]:
        return self._inner.get_last_prices(symbols)

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
