"""Shared test fixtures."""

from __future__ import annotations

import copy
import os
from dataclasses import replace
from datetime import date, timedelta
from typing import Dict, List, Optional

import pytest

from src.config import Config, get_config
from src.journal.db import truncate_all
from src.models import (
    FactorScores,
    FundamentalSnapshot,
    PortfolioState,
    Position,
    TechnicalSnapshot,
)

# Point this at a scratch PostgreSQL database to run the whole suite against the
# backend the fund actually uses, for example:
#   createdb mutualfund_test
#   FUND_TEST_DB_URL=postgresql:///mutualfund_test python -m pytest
TEST_DB_URL_ENV = "FUND_TEST_DB_URL"


@pytest.fixture(scope="session")
def config():
    return get_config()


@pytest.fixture
def temp_config(config, tmp_path) -> Config:
    """A config pointed at a throwaway journal and data directory.

    Defaults to a SQLite file under ``tmp_path`` so the suite needs no server.
    The real ``DATABASE_URL`` is deliberately stripped from the secrets: without
    that, every test here would run against the live journal and truncate it.
    """
    raw = copy.deepcopy(config.raw)
    database = raw.setdefault("database", {})
    database["path"] = str(tmp_path / "fund.db")

    url = (os.getenv(TEST_DB_URL_ENV) or "").strip() or None
    database["url"] = url
    secrets = replace(config.secrets, database_url=url)

    temp = Config(raw=raw, secrets=secrets, root=tmp_path)
    # The universe file lives under the real root, so keep an absolute path to it.
    raw["universe"]["file"] = str(config.path("universe.file"))

    if url:
        # A server-backed database is shared between tests, unlike a tmp file.
        truncate_all(temp)
    return temp


def make_position(
    symbol: str,
    quantity: int = 100,
    avg_cost: float = 100.0,
    last_price: float = 100.0,
    sector: str = "Information Technology",
    entry_days_ago: int = 400,
    peak_price: Optional[float] = None,
    entry_pe: Optional[float] = None,
    target_price: Optional[float] = None,
) -> Position:
    return Position(
        symbol=symbol,
        quantity=quantity,
        avg_cost=avg_cost,
        last_price=last_price,
        sector=sector,
        company_name=f"{symbol} Ltd",
        entry_date=date.today() - timedelta(days=entry_days_ago),
        peak_price=peak_price if peak_price is not None else max(avg_cost, last_price),
        entry_pe=entry_pe,
        target_price=target_price,
    )


def make_portfolio(
    positions: List[Position], cash: float = 100_000.0
) -> PortfolioState:
    return PortfolioState(as_of=date.today(), cash=cash, positions=positions)


def make_technical(
    symbol: str,
    last_price: float = 100.0,
    turnover: float = 1e9,
    volatility: float = 25.0,
    rsi: float = 50.0,
    above_sma_200: bool = True,
) -> TechnicalSnapshot:
    return TechnicalSnapshot(
        symbol=symbol,
        as_of=date.today(),
        last_price=last_price,
        sma_50=last_price * 0.98,
        sma_200=last_price * 0.95,
        rsi_14=rsi,
        atr_14=last_price * 0.02,
        atr_pct=2.0,
        return_6m=10.0,
        return_12m=15.0,
        volatility_annualised=volatility,
        avg_daily_turnover=turnover,
        above_sma_200=above_sma_200,
        golden_cross=True,
    )


def make_fundamental(
    symbol: str,
    pe: Optional[float] = 20.0,
    roe: Optional[float] = 18.0,
    debt_to_equity: Optional[float] = 0.4,
    revenue_growth: Optional[float] = 12.0,
) -> FundamentalSnapshot:
    return FundamentalSnapshot(
        symbol=symbol,
        pe_ratio=pe,
        roe_pct=roe,
        roce_pct=22.0,
        debt_to_equity=debt_to_equity,
        revenue_growth_pct=revenue_growth,
        earnings_growth_pct=14.0,
        operating_margin_pct=20.0,
        market_cap=5e11,
    )


def make_scores(
    symbol: str,
    composite: float = 1.0,
    passes_gates: bool = True,
    failures: Optional[List[str]] = None,
    percentile: float = 85.0,
) -> FactorScores:
    return FactorScores(
        symbol=symbol,
        quality=composite,
        value=composite,
        growth=composite,
        momentum=composite,
        trend=composite,
        composite=composite,
        percentile=percentile,
        passes_gates=passes_gates,
        gate_failures=failures or [],
    )


# A realistic fund shape: enough positions that each sits under the 8% cap and
# no sector exceeds 25%. Used as the baseline for "nothing should fire" tests.
DIVERSIFIED_HOLDINGS = [
    ("TCS", "Information Technology"),
    ("INFY", "Information Technology"),
    ("RELIANCE", "Oil Gas & Consumable Fuels"),
    ("HDFCBANK", "Financial Services"),
    ("ICICIBANK", "Financial Services"),
    ("ITC", "Fast Moving Consumer Goods"),
    ("HINDUNILVR", "Fast Moving Consumer Goods"),
    ("SUNPHARMA", "Healthcare"),
    ("CIPLA", "Healthcare"),
    ("MARUTI", "Automobile and Auto Components"),
    ("ULTRACEMCO", "Construction Materials"),
    ("TATASTEEL", "Metals & Mining"),
    ("LT", "Capital Goods"),
    ("TITAN", "Consumer Durables"),
]


def make_diversified_portfolio(cash: float = 100_000.0) -> PortfolioState:
    """14 equal positions of Rs 100,000 each, plus cash."""
    positions = [
        make_position(symbol, quantity=1000, avg_cost=100.0, last_price=100.0, sector=sector)
        for symbol, sector in DIVERSIFIED_HOLDINGS
    ]
    return make_portfolio(positions, cash=cash)


def diversified_symbols() -> List[str]:
    return [symbol for symbol, _ in DIVERSIFIED_HOLDINGS]


@pytest.fixture
def diversified_technicals() -> Dict[str, TechnicalSnapshot]:
    return {symbol: make_technical(symbol) for symbol in diversified_symbols()}


@pytest.fixture
def diversified_scores() -> Dict[str, FactorScores]:
    return {symbol: make_scores(symbol) for symbol in diversified_symbols()}


@pytest.fixture
def diversified_fundamentals() -> Dict[str, FundamentalSnapshot]:
    return {symbol: make_fundamental(symbol) for symbol in diversified_symbols()}


@pytest.fixture
def technicals() -> Dict[str, TechnicalSnapshot]:
    return {
        symbol: make_technical(symbol)
        for symbol in ("TCS", "INFY", "RELIANCE", "HDFCBANK", "ITC", "WIPRO")
    }


@pytest.fixture
def scores() -> Dict[str, FactorScores]:
    return {
        symbol: make_scores(symbol)
        for symbol in ("TCS", "INFY", "RELIANCE", "HDFCBANK", "ITC", "WIPRO")
    }
