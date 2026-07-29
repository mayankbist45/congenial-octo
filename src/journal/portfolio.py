"""Portfolio assembly.

The broker knows quantity, average cost and current price. It does not know why
a position was bought, what it was worth at its peak, or what the P/E was on
entry - and the sell triggers need all three. This module joins broker holdings
to the fund's own journal to produce the enriched portfolio state the rest of
the pipeline works with.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Dict, List, Optional

from src.broker.base import BrokerClient, BrokerError
from src.config import Config, get_config
from src.journal.repository import JournalRepository
from src.logging_setup import get_logger
from src.models import PortfolioState, Position, TechnicalSnapshot
from src.universe import Universe, load_universe

logger = get_logger(__name__)


def _parse_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)).date()
    except ValueError:
        return None


class PortfolioBuilder:
    """Builds the enriched portfolio snapshot for a run."""

    def __init__(
        self,
        config: Optional[Config] = None,
        universe: Optional[Universe] = None,
    ):
        self._config = config or get_config()
        self._universe = universe or load_universe(self._config)
        self._repo = JournalRepository(self._config)

    def build(
        self,
        broker: BrokerClient,
        technicals: Optional[Dict[str, TechnicalSnapshot]] = None,
        fundamentals: Optional[Dict[str, object]] = None,
        as_of: Optional[date] = None,
    ) -> PortfolioState:
        as_of = as_of or date.today()
        technicals = technicals or {}

        try:
            holdings = broker.get_holdings()
        except BrokerError as exc:
            logger.error("Could not fetch holdings: %s", exc)
            holdings = []

        try:
            funds = broker.get_funds()
            cash = funds.available_cash
        except BrokerError as exc:
            logger.error("Could not fetch funds: %s", exc)
            cash = 0.0

        # A brand new fund has no broker cash history; fall back to the
        # configured starting capital so the first run has something to deploy.
        if not holdings and cash <= 0:
            cash = self._config.secrets.initial_capital
            logger.info(
                "No holdings or broker cash found; using configured starting "
                "capital of Rs %s",
                f"{cash:,.0f}",
            )

        metadata = self._repo.position_meta()
        positions: List[Position] = []

        for holding in holdings:
            symbol = holding.symbol.upper()
            technical = technicals.get(symbol)

            # Prefer the freshest close from our own data over the broker's LTP,
            # which can be stale outside market hours.
            last_price = holding.last_price
            if technical and technical.has_price:
                last_price = technical.last_price
            if not last_price or last_price <= 0:
                last_price = holding.average_price

            meta = metadata.get(symbol, {})
            peak = meta.get("peak_price")
            # Ratchet the peak with today's price so the trailing stop stays
            # honest even if the daily update has not run yet.
            peak_price = max(float(peak or 0.0), last_price) or None

            entry_pe = meta.get("entry_pe")
            position = Position(
                symbol=symbol,
                quantity=holding.quantity,
                avg_cost=holding.average_price,
                last_price=last_price,
                sector=self._universe.sector_of(symbol),
                company_name=self._universe.company_name_of(symbol),
                entry_date=_parse_date(meta.get("entry_date")),
                peak_price=peak_price,
                entry_pe=float(entry_pe) if entry_pe else None,
                thesis=str(meta.get("thesis") or ""),
                target_price=(
                    float(meta["target_price"]) if meta.get("target_price") else None
                ),
            )
            positions.append(position)

            # Persist the ratcheted peak so it survives to the next run.
            if peak_price and peak_price > float(peak or 0.0):
                self._repo.upsert_position_meta(symbol, peak_price=peak_price)

        realised = self._repo.total_realised_pnl()
        state = PortfolioState(
            as_of=as_of,
            cash=cash,
            positions=positions,
            realised_pnl_to_date=realised,
        )

        logger.info(
            "Portfolio: %d position(s), invested Rs %s, cash Rs %s (%.1f%%), "
            "total Rs %s",
            len(positions),
            f"{state.invested_value:,.0f}",
            f"{state.cash:,.0f}",
            state.cash_pct,
            f"{state.total_value:,.0f}",
        )
        return state

    def record_entry(
        self,
        symbol: str,
        price: float,
        entry_pe: Optional[float] = None,
        thesis: str = "",
        target_price: Optional[float] = None,
        entry_date: Optional[date] = None,
    ) -> None:
        """Capture entry context when a new position is opened."""
        self._repo.upsert_position_meta(
            symbol=symbol,
            entry_date=entry_date or date.today(),
            entry_price=price,
            entry_pe=entry_pe,
            peak_price=price,
            thesis=thesis,
            target_price=target_price,
        )
        logger.info("Recorded entry context for %s at %.2f", symbol, price)

    def clear_exited(self, symbol: str) -> None:
        self._repo.clear_position_meta(symbol)
