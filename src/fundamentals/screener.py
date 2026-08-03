"""Screener.in fundamentals scraper.

Screener.in is the practical source for Indian fundamentals: it publishes ROE,
ROCE, promoter holding and multi-year growth for every listed company, which
Yahoo Finance does not reliably carry for NSE names.

Consolidated statements are preferred, with standalone as a fallback for
companies that report only standalone numbers.
"""

from __future__ import annotations

import time
import calendar
from datetime import date, datetime
from typing import Dict, List, Optional, Sequence, Tuple

import requests
from bs4 import BeautifulSoup

from src.fundamentals.parsing import (
    clean_crore,
    clean_number,
    clean_percent,
    growth_pct,
    last_non_null,
    normalise_label,
    safe_divide,
)
from src.fundamentals.sector_models import apply_sector_context
from src.logging_setup import get_logger
from src.models import FundamentalSnapshot, QuarterlyReport

logger = get_logger(__name__)

BASE_URL = "https://www.screener.in/company"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}


class ScreenerClient:
    """Fetches and parses one company page at a time."""

    def __init__(self, request_delay: float = 0.5, timeout: int = 30):
        self._delay = request_delay
        self._timeout = timeout
        self._session = requests.Session()
        self._session.headers.update(HEADERS)
        self._last_request_at = 0.0

    def _throttle(self) -> None:
        elapsed = time.time() - self._last_request_at
        if elapsed < self._delay:
            time.sleep(self._delay - elapsed)
        self._last_request_at = time.time()

    def _fetch_html(self, symbol: str) -> Optional[str]:
        # Consolidated statements first: they reflect the whole group, which is
        # what matters for holding companies. Standalone is the fallback for
        # companies with no subsidiaries.
        for suffix in ("/consolidated/", "/"):
            url = f"{BASE_URL}/{symbol.upper()}{suffix}"

            for attempt in range(1, 4):
                self._throttle()
                try:
                    response = self._session.get(url, timeout=self._timeout)
                except requests.RequestException as exc:
                    logger.debug("Screener request failed for %s: %s", symbol, exc)
                    break

                if response.status_code == 404:
                    break
                if response.status_code == 429:
                    # Back off on the same URL rather than burning the fallback
                    # request, which would also be rate limited.
                    backoff = 3.0 * attempt
                    logger.debug(
                        "Screener rate limited on %s; waiting %.0fs", symbol, backoff
                    )
                    time.sleep(backoff)
                    self._adapt_delay()
                    continue
                if response.ok and "top-ratios" in response.text:
                    return response.text
                break

        logger.debug("No Screener page found for %s", symbol)
        return None

    def _adapt_delay(self) -> None:
        """Slow down permanently after a rate limit, up to a ceiling."""
        if self._delay < 3.0:
            self._delay = min(self._delay * 1.5 + 0.2, 3.0)
            logger.info("Increased Screener request delay to %.2fs", self._delay)

    # ------------------------------------------------------------------
    # Section parsers
    # ------------------------------------------------------------------
    @staticmethod
    def _market_cap_bucket(market_cap: Optional[float]) -> Optional[str]:
        if market_cap is None:
            return None
        if market_cap >= 2e11:
            return "large"
        if market_cap >= 5e10:
            return "mid"
        return "small"

    @staticmethod
    def _parse_top_ratios(soup: BeautifulSoup) -> Dict[str, Optional[float]]:
        """The headline ratio strip at the top of the page."""
        out: Dict[str, Optional[float]] = {}
        container = soup.select_one("#top-ratios")
        if container is None:
            return out

        for item in container.select("li"):
            name_node = item.select_one(".name")
            value_node = item.select_one(".value")
            if name_node is None or value_node is None:
                continue
            label = normalise_label(name_node.get_text())
            text = " ".join(value_node.get_text().split())

            if label == "market cap":
                out["market_cap"] = clean_crore(text)
            elif label == "current price":
                out["current_price"] = clean_number(text)
            elif label in ("stock p/e", "p/e"):
                out["pe_ratio"] = clean_number(text)
            elif label == "book value":
                out["book_value"] = clean_number(text)
            elif label == "dividend yield":
                out["dividend_yield"] = clean_percent(text)
            elif label == "roce":
                out["roce_pct"] = clean_percent(text)
            elif label == "roe":
                out["roe_pct"] = clean_percent(text)
        return out

    @staticmethod
    def _parse_table(soup: BeautifulSoup, section_id: str) -> Dict[str, List[Optional[float]]]:
        """Parse a financial statement table into label -> yearly values."""
        out: Dict[str, List[Optional[float]]] = {}
        section = soup.select_one(f"#{section_id}")
        if section is None:
            return out

        for row in section.select("tbody tr"):
            cells = row.select("td")
            if len(cells) < 2:
                continue
            label = normalise_label(cells[0].get_text())
            if not label:
                continue
            values = [clean_number(cell.get_text()) for cell in cells[1:]]
            # Shareholding tables repeat labels for quarterly and yearly
            # blocks; the first occurrence is the quarterly series we want.
            if label not in out:
                out[label] = values
        return out

    @staticmethod
    def _parse_table_with_headers(
        soup: BeautifulSoup, section_ids: Sequence[str]
    ) -> Tuple[List[str], Dict[str, List[Optional[float]]]]:
        """Parse a table preserving its column headers."""
        section = None
        for section_id in section_ids:
            section = soup.select_one(f"#{section_id}")
            if section is not None:
                break
        if section is None:
            return [], {}

        headers = []
        for cell in section.select("thead th")[1:]:
            label = " ".join(cell.get_text().split())
            if label:
                headers.append(label)

        rows: Dict[str, List[Optional[float]]] = {}
        for row in section.select("tbody tr"):
            cells = row.select("td")
            if len(cells) < 2:
                continue
            label = normalise_label(cells[0].get_text())
            if not label:
                continue
            rows[label] = [clean_number(cell.get_text()) for cell in cells[1:]]
        return headers, rows

    @staticmethod
    def _period_end(label: str) -> Optional[date]:
        cleaned = " ".join(label.replace("'", "").split())
        for fmt in ("%b %Y", "%B %Y"):
            try:
                parsed = datetime.strptime(cleaned, fmt)
                last_day = calendar.monthrange(parsed.year, parsed.month)[1]
                return date(parsed.year, parsed.month, last_day)
            except ValueError:
                continue
        return None

    @staticmethod
    def _parse_growth_ranges(soup: BeautifulSoup) -> Dict[str, Dict[str, Optional[float]]]:
        """Parse the four compounded-growth tables (sales, profit, price, ROE)."""
        out: Dict[str, Dict[str, Optional[float]]] = {}
        for table in soup.select("table.ranges-table"):
            header = table.select_one("th")
            if header is None:
                continue
            key = normalise_label(header.get_text())
            periods: Dict[str, Optional[float]] = {}
            for row in table.select("tr"):
                cells = row.select("td")
                if len(cells) < 2:
                    continue
                period = normalise_label(cells[0].get_text()).rstrip(":")
                periods[period] = clean_percent(cells[1].get_text())
            if periods:
                out[key] = periods
        return out

    # ------------------------------------------------------------------
    # Assembly
    # ------------------------------------------------------------------
    def fetch(self, symbol: str) -> Optional[FundamentalSnapshot]:
        html = self._fetch_html(symbol)
        if html is None:
            return None

        soup = BeautifulSoup(html, "lxml")
        top = self._parse_top_ratios(soup)
        profit_loss = self._parse_table(soup, "profit-loss")
        balance_sheet = self._parse_table(soup, "balance-sheet")
        cash_flow = self._parse_table(soup, "cash-flow")
        ratios = self._parse_table(soup, "ratios")
        shareholding = self._parse_table(soup, "shareholding")
        growth = self._parse_growth_ranges(soup)

        snapshot = FundamentalSnapshot(
            symbol=symbol.upper(),
            as_of=datetime.utcnow(),
            source="screener.in",
            market_cap=top.get("market_cap"),
            pe_ratio=top.get("pe_ratio"),
            dividend_yield=top.get("dividend_yield"),
            roe_pct=top.get("roe_pct"),
            roce_pct=top.get("roce_pct"),
        )
        snapshot.market_cap_bucket = self._market_cap_bucket(snapshot.market_cap)

        # --- Valuation ---
        book_value = top.get("book_value")
        price = top.get("current_price")
        snapshot.price_to_book = safe_divide(price, book_value)
        if snapshot.pe_ratio and snapshot.pe_ratio > 0:
            snapshot.earnings_yield = 100.0 / snapshot.pe_ratio

        # --- Profitability from the P&L ---
        # The last column is usually TTM, which is the freshest read.
        sales = profit_loss.get("sales") or profit_loss.get("revenue") or []
        operating_profit = profit_loss.get("operating profit") or []
        net_profit = profit_loss.get("net profit") or []
        eps_series = profit_loss.get("eps in rs") or []
        interest = profit_loss.get("interest") or []
        opm = profit_loss.get("opm %") or []

        latest_sales = last_non_null(sales)
        latest_net_profit = last_non_null(net_profit)
        latest_operating_profit = last_non_null(operating_profit)

        snapshot.eps = last_non_null(eps_series)
        snapshot.operating_margin_pct = last_non_null(opm)
        snapshot.ebitda_margin_pct = snapshot.operating_margin_pct
        snapshot.ebit_margin_pct = snapshot.operating_margin_pct
        if snapshot.operating_margin_pct is None:
            margin = safe_divide(latest_operating_profit, latest_sales)
            snapshot.operating_margin_pct = margin * 100.0 if margin else None

        profit_margin = safe_divide(latest_net_profit, latest_sales)
        snapshot.profit_margin_pct = profit_margin * 100.0 if profit_margin else None

        latest_interest = last_non_null(interest)
        snapshot.interest_coverage = safe_divide(
            latest_operating_profit, latest_interest
        )
        snapshot.order_book = latest_sales
        snapshot.book_to_bill = 1.0 if latest_sales else None

        # --- Leverage from the balance sheet ---
        borrowings = last_non_null(balance_sheet.get("borrowings") or [])
        reserves = last_non_null(balance_sheet.get("reserves") or [])
        equity_capital = last_non_null(balance_sheet.get("equity capital") or [])
        if borrowings is not None and reserves is not None:
            net_worth = reserves + (equity_capital or 0.0)
            snapshot.debt_to_equity = safe_divide(borrowings, net_worth)
            snapshot.net_debt_to_ebitda = safe_divide(
                borrowings, latest_operating_profit
            )

        # --- Cash quality ---
        free_cash_flow = last_non_null(cash_flow.get("free cash flow") or [])
        snapshot.free_cash_flow = (
            free_cash_flow * 1e7 if free_cash_flow is not None else None
        )
        if free_cash_flow is not None and latest_sales:
            snapshot.free_cash_flow_margin_pct = safe_divide(
                free_cash_flow, latest_sales
            )
            if snapshot.free_cash_flow_margin_pct is not None:
                snapshot.free_cash_flow_margin_pct *= 100.0
        cash_conversion = last_non_null(cash_flow.get("cfo/op") or [])
        if cash_conversion is None:
            operating_cash = last_non_null(
                cash_flow.get("cash from operating activity") or []
            )
            cash_conversion = safe_divide(operating_cash, latest_operating_profit)
        snapshot.cash_conversion = cash_conversion
        if snapshot.cash_conversion is not None:
            snapshot.cfo_to_pat_pct = snapshot.cash_conversion * 100.0

        if snapshot.roce_pct is None:
            snapshot.roce_pct = last_non_null(ratios.get("roce %") or [])

        # --- Growth: prefer TTM/1-year compounded figures over raw YoY ---
        sales_growth = growth.get("compounded sales growth") or {}
        profit_growth = growth.get("compounded profit growth") or {}
        snapshot.revenue_growth_pct = (
            sales_growth.get("ttm")
            if sales_growth.get("ttm") is not None
            else sales_growth.get("3 years")
        )
        snapshot.revenue_cagr_pct = sales_growth.get("3 years")
        snapshot.earnings_growth_pct = (
            profit_growth.get("ttm")
            if profit_growth.get("ttm") is not None
            else profit_growth.get("3 years")
        )

        if snapshot.revenue_growth_pct is None and len(sales) >= 2:
            snapshot.revenue_growth_pct = growth_pct(sales[-1], sales[-2])
        if snapshot.earnings_growth_pct is None and len(net_profit) >= 2:
            snapshot.earnings_growth_pct = growth_pct(net_profit[-1], net_profit[-2])
        if len(sales) >= 3 and sales[-2] is not None and sales[-3] is not None:
            last_growth = growth_pct(sales[-1], sales[-2])
            prior_growth = growth_pct(sales[-2], sales[-3])
            if last_growth is not None and prior_growth is not None:
                snapshot.revenue_acceleration_pct = last_growth - prior_growth

        # PEG only means something with positive growth and a positive P/E.
        if (
            snapshot.pe_ratio
            and snapshot.earnings_growth_pct
            and snapshot.earnings_growth_pct > 0
        ):
            snapshot.peg_ratio = snapshot.pe_ratio / snapshot.earnings_growth_pct

        # --- Ownership: promoter holding and its trend ---
        promoters = shareholding.get("promoters") or []
        snapshot.promoter_holding_pct = last_non_null(promoters)
        snapshot.promoter_ownership_pct = snapshot.promoter_holding_pct
        pledged = shareholding.get("pledged") or []
        snapshot.promoter_pledge_pct = last_non_null(pledged)
        if len(promoters) >= 2:
            latest = promoters[-1]
            previous = promoters[-2]
            if latest is not None and previous is not None:
                snapshot.promoter_ownership_change_pct = latest - previous

        quarter_headers, _ = self._parse_table_with_headers(
            soup, ("quarters", "quarterly-results")
        )
        if quarter_headers:
            snapshot.latest_quarter_label = quarter_headers[-1]
        snapshot.valuation_notes.append("DCF and reverse-DCF require external assumptions; placeholders only")
        snapshot.data_gaps.extend(
            [
                "consensus revisions unavailable from Screener",
                "ownership crowding unavailable from Screener",
            ]
        )
        snapshot = apply_sector_context(snapshot, None)

        return snapshot

    def fetch_quarterly_reports(self, symbol: str) -> List[QuarterlyReport]:
        """Parse quarterly financial rows for one symbol."""
        html = self._fetch_html(symbol)
        if html is None:
            return []

        soup = BeautifulSoup(html, "lxml")
        periods, quarter_rows = self._parse_table_with_headers(
            soup, ("quarters", "quarterly-results")
        )
        if not periods:
            return []

        _, shareholding_rows = self._parse_table_with_headers(soup, ("shareholding",))
        _, balance_sheet_rows = self._parse_table_with_headers(
            soup, ("balance-sheet",)
        )

        reports: List[QuarterlyReport] = []
        fetched_at = datetime.utcnow()
        row_map = {
            "revenue": quarter_rows.get("sales") or quarter_rows.get("revenue") or [],
            "operating_profit": quarter_rows.get("operating profit") or [],
            "opm_pct": quarter_rows.get("opm %") or [],
            "net_profit": quarter_rows.get("net profit") or [],
            "eps": quarter_rows.get("eps in rs") or [],
            "interest": quarter_rows.get("interest") or [],
            "borrowings": balance_sheet_rows.get("borrowings") or [],
            "promoter_holding_pct": shareholding_rows.get("promoters") or [],
            "promoter_pledge_pct": shareholding_rows.get("pledged") or [],
        }

        for index, period in enumerate(periods):
            reports.append(
                QuarterlyReport(
                    symbol=symbol.upper(),
                    fiscal_period=period,
                    report_key=f"{symbol.upper()}:{period}",
                    source="screener.in",
                    fetched_at=fetched_at,
                    period_end=self._period_end(period),
                    revenue=row_map["revenue"][index] if index < len(row_map["revenue"]) else None,
                    operating_profit=row_map["operating_profit"][index] if index < len(row_map["operating_profit"]) else None,
                    opm_pct=row_map["opm_pct"][index] if index < len(row_map["opm_pct"]) else None,
                    net_profit=row_map["net_profit"][index] if index < len(row_map["net_profit"]) else None,
                    eps=row_map["eps"][index] if index < len(row_map["eps"]) else None,
                    interest=row_map["interest"][index] if index < len(row_map["interest"]) else None,
                    borrowings=row_map["borrowings"][index] if index < len(row_map["borrowings"]) else None,
                    promoter_holding_pct=(
                        row_map["promoter_holding_pct"][index]
                        if index < len(row_map["promoter_holding_pct"])
                        else None
                    ),
                    promoter_pledge_pct=(
                        row_map["promoter_pledge_pct"][index]
                        if index < len(row_map["promoter_pledge_pct"])
                        else None
                    ),
                )
            )
        return reports

    def close(self) -> None:
        self._session.close()
