"""Tests for journal-backed paper books behind live market-data clients."""

from __future__ import annotations

from datetime import date
from typing import Dict, List, Optional

from src.broker.base import BrokerClient, BrokerFunds, BrokerHolding, OrderRequest, OrderResult
from src.broker.dry_run import DryRunBroker
from src.broker.simulated import SimulatedBroker
from src.dashboard.service import DashboardService
from src.journal.portfolio import PortfolioBuilder
from src.journal.repository import JournalRepository
from tests.conftest import make_technical


class QuoteBroker(BrokerClient):
    """A broker with deliberately different account state and quote state."""

    @property
    def name(self) -> str:
        return "quotes"

    def connect(self) -> bool:
        return True

    def get_holdings(self) -> List[BrokerHolding]:
        return [BrokerHolding("LIVEONLY", 99, 10.0, 10.0)]

    def get_funds(self) -> BrokerFunds:
        return BrokerFunds(available_cash=123.0, net_value=123.0)

    def get_last_price(self, symbol: str) -> Optional[float]:
        return {"WIPRO": 110.0}.get(symbol.upper())

    def get_last_prices(self, symbols: List[str]) -> Dict[str, float]:
        return {
            symbol.upper(): 110.0
            for symbol in symbols
            if symbol.upper() == "WIPRO"
        }

    def place_order(self, request: OrderRequest) -> OrderResult:
        raise AssertionError("DryRunBroker must suppress order placement")


def test_journal_paper_book_drives_holdings_cash_and_dashboard(temp_config) -> None:
    repo = JournalRepository(temp_config)
    repo.record_trade(
        symbol="WIPRO",
        action="BUY",
        quantity=10,
        price=100.0,
        status="dry_run",
        dry_run=True,
    )
    quotes = QuoteBroker()
    broker = DryRunBroker(
        quotes,
        paper_book=SimulatedBroker(temp_config, price_lookup=quotes),
    )

    holdings = broker.get_holdings()
    assert [(holding.symbol, holding.quantity, holding.last_price) for holding in holdings] == [
        ("WIPRO", 10.0, 110.0)
    ]
    assert broker.get_funds().available_cash == temp_config.secrets.initial_capital - 1_000.0
    assert broker.get_last_price("WIPRO") == 110.0

    portfolio = PortfolioBuilder(temp_config).build(
        broker,
        technicals={"WIPRO": make_technical("WIPRO", last_price=110.0)},
    )
    repo.record_position_snapshot(date.today(), portfolio)

    dashboard_holdings = DashboardService(temp_config).holdings()
    assert [(row["symbol"], row["quantity"]) for row in dashboard_holdings] == [
        ("WIPRO", 10.0)
    ]
