"""Yahoo Finance fundamentals.

Used to fill gaps Screener.in does not cover: forward P/E, EV/EBITDA, analyst
target prices and consensus ratings.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from src.logging_setup import get_logger
from src.models import FundamentalSnapshot

logger = get_logger(__name__)


def _pct(value: Optional[float]) -> Optional[float]:
    """Yahoo reports ratios as fractions; the fund works in percent."""
    if value is None:
        return None
    try:
        return float(value) * 100.0
    except (TypeError, ValueError):
        return None


def _num(value: Optional[object]) -> Optional[float]:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result


def fetch(symbol: str) -> Optional[FundamentalSnapshot]:
    import yfinance as yf

    try:
        info = yf.Ticker(f"{symbol.upper()}.NS").info or {}
    except Exception as exc:
        logger.debug("Yahoo fundamentals failed for %s: %s", symbol, exc)
        return None

    if not info or info.get("trailingPE") is None and info.get("marketCap") is None:
        return None

    snapshot = FundamentalSnapshot(
        symbol=symbol.upper(),
        as_of=datetime.utcnow(),
        source="yahoo",
        market_cap=_num(info.get("marketCap")),
        pe_ratio=_num(info.get("trailingPE")),
        forward_pe=_num(info.get("forwardPE")),
        peg_ratio=_num(info.get("pegRatio")),
        price_to_book=_num(info.get("priceToBook")),
        ev_to_ebitda=_num(info.get("enterpriseToEbitda")),
        dividend_yield=_num(info.get("dividendYield")),
        roe_pct=_pct(info.get("returnOnEquity")),
        operating_margin_pct=_pct(info.get("operatingMargins")),
        profit_margin_pct=_pct(info.get("profitMargins")),
        # Yahoo already reports debtToEquity in percent for Indian names.
        debt_to_equity=(
            _num(info.get("debtToEquity")) / 100.0
            if _num(info.get("debtToEquity")) is not None
            else None
        ),
        current_ratio=_num(info.get("currentRatio")),
        revenue_growth_pct=_pct(info.get("revenueGrowth")),
        earnings_growth_pct=_pct(info.get("earningsGrowth")),
        eps=_num(info.get("trailingEps")),
        free_cash_flow=_num(info.get("freeCashflow")),
        promoter_holding_pct=_pct(info.get("heldPercentInsiders")),
        analyst_target_price=_num(info.get("targetMeanPrice")),
        analyst_recommendation=info.get("recommendationKey") or None,
    )
    return snapshot
