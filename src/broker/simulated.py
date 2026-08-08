"""Simulated broker.

Used before the Angel One account is wired up, and whenever ``DRY_RUN=true``.
Holdings and cash come from the local trade journal instead of the broker, and
prices come from the market data layer, so the whole daily cycle can be
exercised end to end without touching a real account.
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
from src.config import Config, get_config
from src.logging_setup import get_logger

logger = get_logger(__name__)


class SimulatedBroker(BrokerClient):
    """Paper broker whose state is reconstructed from journalled trades."""

    def __init__(
        self,
        config: Optional[Config] = None,
        price_lookup: Optional[object] = None,
    ):
        self._config = config or get_config()
        # A callable or object exposing ``get_last_prices``; injected by the
        # factory so this module does not depend on the market data package.
        self._price_lookup = price_lookup
        self._connected = False

    @property
    def name(self) -> str:
        return "Simulated"

    def connect(self) -> bool:
        self._connected = True
        logger.info("Simulated broker active - no real orders will be placed")
        return True

    def get_holdings(self) -> List[BrokerHolding]:
        """Rebuild holdings from journalled fills."""
        from src.journal.repository import JournalRepository

        repo = JournalRepository(self._config)
        lots = repo.reconstruct_positions()
        if not lots:
            return []

        prices = self.get_last_prices([lot["symbol"] for lot in lots])
        holdings: List[BrokerHolding] = []
        for lot in lots:
            symbol = lot["symbol"]
            last_price = prices.get(symbol) or lot["avg_cost"]
            holdings.append(
                BrokerHolding(
                    symbol=symbol,
                    quantity=float(lot["quantity"]),
                    average_price=float(lot["avg_cost"]),
                    last_price=float(last_price),
                )
            )
        return holdings

    def get_funds(self) -> BrokerFunds:
        """Cash equals starting capital minus net invested, plus realised P&L."""
        from src.journal.repository import JournalRepository

        repo = JournalRepository(self._config)
        net_invested = repo.net_cash_deployed()
        realised = repo.total_realised_pnl()
        cash = self._config.secrets.initial_capital - net_invested + realised
        return BrokerFunds(available_cash=max(cash, 0.0), net_value=max(cash, 0.0))

    def get_last_price(self, symbol: str) -> Optional[float]:
        prices = self.get_last_prices([symbol])
        return prices.get(symbol.upper())

    def get_last_prices(self, symbols: List[str]) -> Dict[str, float]:
        if not symbols:
            return {}
        if self._price_lookup is None:
            logger.warning("Simulated broker has no price source configured")
            return {}
        getter = getattr(self._price_lookup, "get_last_prices", None)
        if getter is None:
            return {}
        return getter(symbols)

    def place_order(self, request: OrderRequest) -> OrderResult:
        """Fill instantly at the limit price (or last price for market orders)."""
        price = request.price or self.get_last_price(request.symbol)
        if not price:
            return OrderResult(
                success=False,
                symbol=request.symbol,
                side=request.side,
                quantity=request.quantity,
                error=f"No price available for {request.symbol}",
                dry_run=True,
            )

        logger.info(
            "[SIMULATED] %s %s x%s @ %.2f",
            request.side,
            request.symbol,
            _format_quantity(request.quantity),
            price,
        )
        return OrderResult(
            success=True,
            symbol=request.symbol,
            side=request.side,
            quantity=request.quantity,
            order_id=f"SIM-{request.symbol}-{request.side}-{request.quantity}",
            price=float(price),
            status="filled",
            dry_run=True,
        )


def _format_quantity(quantity: float) -> str:
    if abs(quantity - round(quantity)) < 1e-9:
        return str(int(round(quantity)))
    return f"{quantity:.8f}".rstrip("0").rstrip(".")
