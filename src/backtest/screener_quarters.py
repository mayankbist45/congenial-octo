"""Indian quarterly results parsed from Screener.in company pages.

Screener publishes a ``#quarters`` table per company (Sales, Operating Profit,
OPM %, Net Profit, EPS in Rs, with columns like "Jun 2025"), consolidated
first with standalone as the fallback — the same preference as the fund's
live fundamentals scraper.

What this module deliberately does *not* have is a reliable results
announcement date. Screener shows only a short list of recent announcements
(linked out to BSE PDFs), not a history, and full history needs a BSE crawl
this project has not signed up for. The parser captures whatever recent
results-related announcements the page exposes, saves them per ticker, and
the coverage report says honestly what fraction of the universe has one.
Where the date is missing the backtest falls back to ``period_end + 45 days``
(the SEBI-mandated outer limit for results publication).

Throttling mirrors the live scraper's etiquette: a 1s+ delay between
requests, exponential backoff on HTTP 429, and a permanently raised delay
after any rate limit. Values are kept in Screener's native units — rupee
crore for flows, rupees for EPS — which the column names record.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
import requests
from bs4 import BeautifulSoup

from src.fundamentals.parsing import clean_number, normalise_label
from src.fundamentals.screener import BASE_URL, HEADERS
from src.logging_setup import get_logger

logger = get_logger(__name__)

QUARTERS_COLUMNS = [
    "quarter_label",
    "period_end",
    "sales_cr",
    "operating_profit_cr",
    "opm_pct",
    "net_profit_cr",
    "eps_rs",
]

# Row label -> tidy column. Screener labels are stable across companies.
_LABEL_MAP = {
    "sales": "sales_cr",
    "operating profit": "operating_profit_cr",
    "opm %": "opm_pct",
    "opm": "opm_pct",
    "net profit": "net_profit_cr",
    "eps in rs": "eps_rs",
}

_DATE_PATTERNS = (
    re.compile(r"\b(\d{1,2}\s+[A-Z][a-z]{2}\s+\d{4})\b"),  # 12 May 2025
    re.compile(r"\b(\d{1,2}-[A-Z][a-z]{2}-\d{4})\b"),  # 12-May-2025
    re.compile(r"\b(\d{4}-\d{2}-\d{2})\b"),  # 2025-05-12
)
_DATE_FORMATS = ("%d %b %Y", "%d-%b-%Y", "%Y-%m-%d")
# Screener's recent-announcements list renders dates as "25 Jul" with no year;
# the entry is always within the last ~12 months, so the year is inferable.
_SHORT_DATE_PATTERN = re.compile(r"\b(\d{1,2}\s+[A-Z][a-z]{2})\b")
# The freshest entries render as relative markers instead: "2h", "3d".
_RELATIVE_PATTERN = re.compile(r"^\s*(\d+)\s*([hd])\b")


def _infer_short_date(text: str, today: date) -> Optional[date]:
    match = _SHORT_DATE_PATTERN.search(text)
    if not match:
        return None
    try:
        candidate = pd.to_datetime(
            f"{match.group(1)} {today.year}", format="%d %b %Y"
        ).date()
    except (ValueError, TypeError):
        return None
    if candidate > today:
        try:
            candidate = candidate.replace(year=candidate.year - 1)
        except ValueError:  # Feb 29 on a non-leap wrap
            return None
    return candidate


def quarter_label_to_period_end(label: str) -> Optional[date]:
    """"Jun 2025" -> 2025-06-30. Indian quarters end at calendar month-ends."""
    try:
        stamp = pd.to_datetime(label.strip(), format="%b %Y")
    except (ValueError, TypeError):
        return None
    return (stamp + pd.offsets.MonthEnd(0)).date()


def parse_quarters_html(html: str) -> pd.DataFrame:
    """Parse the #quarters table into one tidy row per reported quarter."""
    soup = BeautifulSoup(html, "lxml")
    section = soup.select_one("#quarters")
    if section is None:
        return pd.DataFrame(columns=QUARTERS_COLUMNS)

    table = section.select_one("table")
    if table is None:
        return pd.DataFrame(columns=QUARTERS_COLUMNS)

    header_cells = table.select("thead th")
    labels = [" ".join(cell.get_text().split()) for cell in header_cells[1:]]

    series: Dict[str, List[Optional[float]]] = {}
    for row in table.select("tbody tr"):
        cells = row.select("td")
        if len(cells) < 2:
            continue
        label = normalise_label(cells[0].get_text())
        column = _LABEL_MAP.get(label)
        if column is None or column in series:
            continue
        values = [clean_number(cell.get_text()) for cell in cells[1:]]
        series[column] = values[: len(labels)]

    if not labels or not series:
        return pd.DataFrame(columns=QUARTERS_COLUMNS)

    frame = pd.DataFrame({"quarter_label": labels})
    for column, values in series.items():
        # Rows occasionally render one cell short; pad rather than misalign.
        padded = values + [None] * (len(labels) - len(values))
        frame[column] = padded[: len(labels)]
    frame["period_end"] = frame["quarter_label"].map(quarter_label_to_period_end)
    frame = frame[frame["period_end"].notna()]
    for column in QUARTERS_COLUMNS:
        if column not in frame.columns:
            frame[column] = None
    return frame[QUARTERS_COLUMNS].reset_index(drop=True)


def _extract_date(text: str, today: Optional[date] = None) -> Optional[date]:
    relative = _RELATIVE_PATTERN.match(text)
    if relative:
        amount, unit = int(relative.group(1)), relative.group(2)
        reference = today or date.today()
        delta = timedelta(hours=amount) if unit == "h" else timedelta(days=amount)
        return (datetime.combine(reference, dtime.min) - delta).date()
    for pattern, fmt in zip(_DATE_PATTERNS, _DATE_FORMATS):
        match = pattern.search(text)
        if match:
            try:
                return pd.to_datetime(match.group(1), format=fmt).date()
            except (ValueError, TypeError):
                continue
    return _infer_short_date(text, today or date.today())


def parse_announcements_html(html: str) -> List[Dict[str, object]]:
    """Whatever recent announcements the page exposes.

    Returns dicts with date/title/url/results_related. Most pages show only
    the last few announcements, so an empty list is normal, not a failure.
    """
    soup = BeautifulSoup(html, "lxml")

    container = soup.select_one("#announcements")
    if container is None:
        heading = soup.find(
            lambda tag: tag.name in ("h2", "h3", "h4")
            and "announcement" in tag.get_text().lower()
        )
        if heading is not None:
            container = heading.find_next(["ul", "div", "section"])
    if container is None:
        return []

    announcements: List[Dict[str, object]] = []
    items = container.select("li") or [container]
    for item in items:
        text = " ".join(item.get_text(" ", strip=True).split())
        if not text:
            continue
        link = item.select_one("a[href]")
        url = str(link.get("href") or "") if link else ""
        if link is not None:
            # The anchor holds the title directly; the date sits in a nested
            # div/span, so only take the anchor's immediate text nodes.
            title = " ".join(
                node.strip()
                for node in link.find_all(string=True, recursive=False)
                if node.strip()
            )
            if not title:
                title = " ".join(link.get_text(" ", strip=True).split())
        else:
            title = text
        # The muted sub-line holds the entry's date; using it avoids summary
        # text like "board meeting on 12 August 2026" (a future date).
        date_node = item.select_one(".ink-600")
        date_text = " ".join(date_node.get_text(" ", strip=True).split()) if date_node else text
        announced = _extract_date(date_text)
        announcements.append(
            {
                "date": announced.isoformat() if announced else "",
                "title": title,
                "url": url,
                "results_related": "result" in title.lower(),
            }
        )
    return announcements


class ScreenerQuarterlyClient:
    """Fetches Screener company pages politely, one at a time."""

    def __init__(self, request_delay: float = 1.2, timeout: int = 30):
        self._delay = request_delay
        self._timeout = timeout
        self._session = requests.Session()
        self._session.headers.update(HEADERS)
        self._last_request_at = 0.0
        self.rate_limited_count = 0

    def _throttle(self) -> None:
        elapsed = time.time() - self._last_request_at
        if elapsed < self._delay:
            time.sleep(self._delay - elapsed)
        self._last_request_at = time.time()

    def _adapt_delay(self) -> None:
        """Slow down permanently after a rate limit, up to a ceiling."""
        self.rate_limited_count += 1
        if self._delay < 4.0:
            self._delay = min(self._delay * 1.5 + 0.2, 4.0)
            logger.info("Increased Screener request delay to %.2fs", self._delay)

    def _get(self, url: str) -> Optional[str]:
        for attempt in range(1, 4):
            self._throttle()
            try:
                response = self._session.get(url, timeout=self._timeout)
            except requests.RequestException as exc:
                logger.debug("Screener request failed for %s: %s", url, exc)
                return None

            if response.status_code == 404:
                return None
            if response.status_code == 429:
                backoff = 3.0 * (2 ** (attempt - 1))
                logger.debug("Screener rate limited on %s; waiting %.0fs", url, backoff)
                time.sleep(backoff)
                self._adapt_delay()
                continue
            if response.ok and "top-ratios" in response.text:
                return response.text
            return None
        return None

    def fetch_page(self, symbol: str) -> Tuple[Optional[str], str]:
        """HTML plus which statement view it came from (consolidated first)."""
        for suffix, flavour in (("/consolidated/", "consolidated"), ("/", "standalone")):
            html = self._get(f"{BASE_URL}/{symbol.upper()}{suffix}")
            if html is not None:
                return html, flavour
        return None, ""

    def fetch_standalone(self, symbol: str) -> Optional[str]:
        """The standalone page, for when consolidated has no usable table."""
        return self._get(f"{BASE_URL}/{symbol.upper()}/")

    def close(self) -> None:
        self._session.close()


@dataclass
class IndiaTickerResult:
    symbol: str
    source: str = ""
    quarters: int = 0
    first_period: Optional[str] = None
    last_period: Optional[str] = None
    announcements_found: int = 0
    results_announcements: int = 0
    latest_results_date: Optional[str] = None
    error: str = ""


def fetch_india_fundamentals(
    symbols: List[str],
    quarterly_dir: Path,
    announcements_dir: Path,
    client: Optional[ScreenerQuarterlyClient] = None,
    resume: bool = True,
) -> List[IndiaTickerResult]:
    """Fetch, parse and cache quarterly results for each NSE symbol.

    One CSV per symbol under ``quarterly_dir``; an announcements CSV under
    ``announcements_dir`` only when the page exposed results-related entries.
    Existing quarterly CSVs are skipped when ``resume`` is set.
    """
    quarterly_dir = Path(quarterly_dir)
    announcements_dir = Path(announcements_dir)
    quarterly_dir.mkdir(parents=True, exist_ok=True)
    announcements_dir.mkdir(parents=True, exist_ok=True)

    own_client = client is None
    client = client or ScreenerQuarterlyClient()

    results: List[IndiaTickerResult] = []
    try:
        for index, raw_symbol in enumerate(symbols, start=1):
            symbol = raw_symbol.upper().strip()
            out_path = quarterly_dir / f"{symbol}.csv"
            if resume and out_path.exists():
                try:
                    cached = pd.read_csv(out_path)
                    results.append(
                        IndiaTickerResult(
                            symbol=symbol,
                            source="cache",
                            quarters=len(cached),
                            first_period=str(cached["period_end"].iloc[0])
                            if len(cached)
                            else None,
                            last_period=str(cached["period_end"].iloc[-1])
                            if len(cached)
                            else None,
                        )
                    )
                except (pd.errors.ParserError, KeyError, OSError):
                    results.append(
                        IndiaTickerResult(symbol=symbol, error="cached unreadable")
                    )
                continue

            result = IndiaTickerResult(symbol=symbol)
            html, flavour = client.fetch_page(symbol)
            if html is None:
                result.error = "no screener page"
                results.append(result)
                continue

            quarters = parse_quarters_html(html)
            if quarters.empty and flavour == "consolidated":
                # Some pages (banks/NBFCs especially) render an empty quarters
                # table consolidated-side; standalone can still carry values.
                standalone_html = client.fetch_standalone(symbol)
                if standalone_html is not None:
                    standalone_quarters = parse_quarters_html(standalone_html)
                    if not standalone_quarters.empty:
                        quarters = standalone_quarters
                        flavour = "standalone"
            result.source = flavour

            if quarters.empty:
                result.error = "no quarters table parsed"
                results.append(result)
                continue

            quarters.to_csv(out_path, index=False)
            result.quarters = len(quarters)
            result.first_period = str(quarters["period_end"].iloc[0])
            result.last_period = str(quarters["period_end"].iloc[-1])

            announcements = parse_announcements_html(html)
            result.announcements_found = len(announcements)
            results_related = [
                entry for entry in announcements if entry.get("results_related")
            ]
            result.results_announcements = len(results_related)
            dated = [str(e["date"]) for e in results_related if e.get("date")]
            if dated:
                result.latest_results_date = max(dated)
                pd.DataFrame(results_related).to_csv(
                    announcements_dir / f"{symbol}.csv", index=False
                )
            results.append(result)

            if index % 20 == 0 or index == len(symbols):
                logger.info("Screener progress: %d/%d symbols", index, len(symbols))
    finally:
        if own_client:
            client.close()

    return results
