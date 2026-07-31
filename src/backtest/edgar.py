"""US quarterly fundamentals from SEC EDGAR XBRL company facts.

EDGAR is the only free source that pairs each reported figure with the date it
was *filed*, and that ``filed`` date is the point-in-time anchor for the whole
backtest: a quarter's numbers are only knowable from the filing date onward,
never from the period end.

Fetch path: ticker -> CIK via the SEC's ``company_tickers.json`` map, then one
``companyfacts`` JSON per CIK from ``data.sec.gov``. Raw payloads are cached on
disk so re-runs only download what is missing.

Frame handling (the part that is easy to get wrong)
---------------------------------------------------
The companyfacts API returns every fact instance ever filed for a tag. For the
flow metrics used here (revenue, net income, operating income, diluted EPS)
each instance has ``start``/``end``/``val``/``filed``/``form`` and sometimes a
``frame`` tag like ``CY2023Q2``. Instances come in several shapes:

- **Standalone quarters** — a 10-Q's three-month column, span ~60-115 days.
  Used directly (``frame_deduced=False``).
- **Year-to-date durations** — 10-Qs also report six- and nine-month columns
  (span ~150-320 days), and the 10-K reports the full year (~330-380 days).
  A standalone Q2/Q3/Q4 is recovered by subtracting the longest shorter
  duration that shares the same ``start`` date: ``Q2 = H1 - Q1``,
  ``Q3 = 9M - H1``, ``Q4 = FY - 9M``. These rows carry ``frame_deduced=True``.
  The residual must itself land in the quarterly span window or it is dropped.
- **Instant facts** (no ``start``) are balance-sheet style point-in-time
  values; they are meaningless for flow metrics and are skipped.

Two honest caveats of the subtraction approach:

- Diluted EPS is not strictly additive across periods (weighted share counts
  change), so deduced EPS quarters are an approximation. They are still marked
  ``frame_deduced`` so the engine can distrust them if it wants to.
- The ``filed`` date of a deduced row is the filing date of the *longer*
  duration fact, because that is when the residual became computable from
  public information — e.g. standalone Q4 is unknowable until the 10-K lands,
  even though the 9M piece was public since the Q3 10-Q.

Deduping: companies refile and amend (10-Q/A, 10-K/A), so the same
(tag, start, end) often appears several times. The first filing per period
wins (earliest ``filed``), on the principle that the backtest trades on what
was known first, not on later restatements.

Merging across metrics: each metric is extracted independently, then rows are
outer-joined on ``period_end``. The row's ``filed`` is the *latest* of the
metric-level filing dates — a quarter is only usable once every metric for it
is public — and ``source_form`` names the form that filed last.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
import requests

from src.logging_setup import get_logger

logger = get_logger(__name__)

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
COMPANY_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
# SEC fair-access policy: a descriptive User-Agent and no more than 10
# requests per second. We stay well under the cap.
USER_AGENT = "congenial-octo research contact@example.com"

# Tags are tried in priority order per period: the modern tag wins where it
# exists, and older tags fill periods they alone cover (pre-2018 revenue is
# usually only under ``Revenues``/``SalesRevenueNet``).
METRIC_TAGS: Dict[str, List[str]] = {
    "revenue": [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
    ],
    "net_income": ["NetIncomeLoss", "ProfitLoss"],
    # Diluted first, basic as the spec-sanctioned fallback where diluted is
    # not tagged; the column name stays eps_diluted for the primary tag.
    "eps_diluted": ["EarningsPerShareDiluted", "EarningsPerShareBasic"],
    "operating_income": ["OperatingIncomeLoss"],
}
METRIC_UNITS: Dict[str, Tuple[str, ...]] = {
    "revenue": ("USD",),
    "net_income": ("USD",),
    "eps_diluted": ("USD/shares",),
    "operating_income": ("USD",),
}

REPORT_FORMS = {"10-Q", "10-K", "10-Q/A", "10-K/A"}

# Span classification windows, in days. 53-week fiscal years and short
# transition quarters are why these are ranges, not exact lengths.
QUARTER_SPAN = (60, 115)
HALF_SPAN = (150, 220)
NINE_MONTH_SPAN = (240, 320)
YEAR_SPAN = (330, 380)

QUARTERLY_COLUMNS = [
    "period_end",
    "filed",
    "revenue",
    "net_income",
    "eps_diluted",
    "operating_income",
    "source_form",
    "frame_deduced",
]


@dataclass
class DurationFact:
    """One filed duration instance of a metric."""

    start: date
    end: date
    value: float
    filed: date
    form: str

    @property
    def span_days(self) -> int:
        return (self.end - self.start).days


def _parse_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def _span_window(days: int) -> Optional[str]:
    if QUARTER_SPAN[0] <= days <= QUARTER_SPAN[1]:
        return "quarter"
    if HALF_SPAN[0] <= days <= HALF_SPAN[1]:
        return "half"
    if NINE_MONTH_SPAN[0] <= days <= NINE_MONTH_SPAN[1]:
        return "nine_month"
    if YEAR_SPAN[0] <= days <= YEAR_SPAN[1]:
        return "year"
    return None


def _collect_duration_facts(tag_payload: dict, units: Tuple[str, ...]) -> List[DurationFact]:
    """Pull every usable duration instance of one tag, deduped to first filing."""
    out: List[DurationFact] = []
    unit_blocks = tag_payload.get("units") or {}
    for unit in units:
        for raw in unit_blocks.get(unit) or []:
            if raw.get("form") not in REPORT_FORMS:
                continue
            start = _parse_date(raw.get("start"))
            end = _parse_date(raw.get("end"))
            filed = _parse_date(raw.get("filed"))
            if start is None or end is None or filed is None:
                # Instant facts carry no start; unusable for flow metrics.
                continue
            if raw.get("val") is None:
                continue
            out.append(
                DurationFact(
                    start=start,
                    end=end,
                    value=float(raw["val"]),
                    filed=filed,
                    form=str(raw.get("form") or ""),
                )
            )

    # First filing per period wins: sort by filed, keep one per (start, end).
    out.sort(key=lambda fact: (fact.filed, fact.form))
    deduped: Dict[Tuple[date, date], DurationFact] = {}
    for fact in out:
        key = (fact.start, fact.end)
        if key not in deduped:
            deduped[key] = fact
    return list(deduped.values())


def standalone_quarters(facts: List[DurationFact]) -> List[Tuple[DurationFact, bool]]:
    """Reduce duration facts to standalone quarters.

    Returns ``(fact, frame_deduced)`` pairs: direct three-month filings first,
    then YTD/annual residuals where the shorter piece exists.
    """
    quarters: Dict[date, Tuple[DurationFact, bool]] = {}
    for fact in facts:
        if _span_window(fact.span_days) == "quarter":
            existing = quarters.get(fact.end)
            if existing is None or fact.filed < existing[0].filed:
                quarters[fact.end] = (fact, False)

    for fact in facts:
        window = _span_window(fact.span_days)
        if window in (None, "quarter"):
            continue
        # Longest shorter duration with the same start: H1 for a 9M column,
        # Q1 for an H1 column, 9M for a 10-K's year.
        candidates = [
            other
            for other in facts
            if other.start == fact.start and other.end < fact.end
        ]
        if not candidates:
            continue
        base = max(candidates, key=lambda other: other.end)
        residual_end = fact.end
        residual_span = (residual_end - base.end).days
        if _span_window(residual_span) != "quarter":
            continue
        residual = DurationFact(
            start=base.end,
            end=residual_end,
            value=fact.value - base.value,
            filed=fact.filed,
            form=fact.form,
        )
        # A direct filing beats a deduction; an earlier deduction beats a later.
        existing = quarters.get(residual_end)
        if existing is None or (existing[1] and fact.filed < existing[0].filed):
            if existing is not None and not existing[1]:
                continue
            quarters[residual_end] = (residual, True)

    return sorted(quarters.values(), key=lambda pair: pair[0].end)


def extract_quarterly(facts_payload: dict) -> Tuple[pd.DataFrame, Dict[str, str]]:
    """Turn one companyfacts payload into the tidy quarterly table.

    Returns the DataFrame (one row per period end, columns per
    ``QUARTERLY_COLUMNS``) and a dict of which XBRL tag supplied each metric,
    for the coverage report.
    """
    gaap = (facts_payload.get("facts") or {}).get("us-gaap") or {}
    today = date.today()
    per_metric: Dict[str, pd.DataFrame] = {}
    tags_used: Dict[str, str] = {}

    for metric, candidate_tags in METRIC_TAGS.items():
        per_period: Dict[pd.Timestamp, dict] = {}
        used_tags: List[str] = []
        for tag in candidate_tags:
            tag_payload = gaap.get(tag)
            if not tag_payload:
                continue
            facts = _collect_duration_facts(tag_payload, METRIC_UNITS[metric])
            if not facts:
                continue
            used_tags.append(tag)
            for fact, deduced in standalone_quarters(facts):
                if fact.filed > today:
                    continue  # a clock or filing anomaly, never tradeable knowledge
                key = pd.Timestamp(fact.end)
                if key not in per_period:
                    per_period[key] = {
                        "period_end": key,
                        "filed": pd.Timestamp(fact.filed),
                        "value": fact.value,
                        "form": fact.form,
                        "deduced": deduced,
                    }
        if per_period:
            tags_used[metric] = "+".join(used_tags)
            metric_frame = pd.DataFrame(
                [per_period[key] for key in sorted(per_period)]
            )
            # YTD subtraction leaves float dust (0.7799999999999994); 6dp is
            # far below reporting precision for all four metrics.
            metric_frame["value"] = metric_frame["value"].round(6)
            per_metric[metric] = metric_frame

    if not per_metric:
        return pd.DataFrame(columns=QUARTERLY_COLUMNS), tags_used

    merged: Optional[pd.DataFrame] = None
    for metric, frame in per_metric.items():
        renamed = frame.rename(
            columns={
                "value": metric,
                "filed": f"filed_{metric}",
                "form": f"form_{metric}",
                "deduced": f"deduced_{metric}",
            }
        )
        merged = (
            renamed
            if merged is None
            else merged.merge(renamed, on="period_end", how="outer")
        )
    assert merged is not None

    filed_cols = [f"filed_{m}" for m in per_metric]
    form_cols = [f"form_{m}" for m in per_metric]
    deduced_cols = [f"deduced_{m}" for m in per_metric]

    for metric in METRIC_TAGS:
        if metric not in merged.columns:
            merged[metric] = pd.NA

    merged["filed"] = merged[filed_cols].max(axis=1)
    merged["source_form"] = merged[form_cols].bfill(axis=1).iloc[:, 0]
    # NaN counts as False here: a missing metric says nothing about deduction.
    merged["frame_deduced"] = merged[deduced_cols].eq(True).any(axis=1)

    # Impossible rows: filed before the period even ended. These come from
    # companies restating an old period inside a new filing's comparatives.
    impossible = merged["filed"] < merged["period_end"]
    if impossible.any():
        logger.debug(
            "Dropping %d row(s) filed before their period end", int(impossible.sum())
        )
        merged = merged[~impossible]

    out = merged[
        ["period_end", "filed"]
        + list(METRIC_TAGS.keys())
        + ["source_form", "frame_deduced"]
    ].sort_values("period_end")
    return out.reset_index(drop=True), tags_used


class EdgarClient:
    """Polite SEC EDGAR client: throttled, cached, retrying once on trouble."""

    def __init__(self, raw_dir: Path, min_interval: float = 0.12, timeout: int = 30):
        self._raw_dir = Path(raw_dir)
        self._raw_dir.mkdir(parents=True, exist_ok=True)
        self._min_interval = min_interval
        self._timeout = timeout
        self._session = requests.Session()
        self._session.headers.update(
            {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"}
        )
        self._last_request_at = 0.0

    def _throttle(self) -> None:
        elapsed = time.time() - self._last_request_at
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_request_at = time.time()

    def _get_json(self, url: str, cache_path: Optional[Path] = None) -> Optional[dict]:
        if cache_path is not None and cache_path.exists():
            try:
                with open(cache_path, "r", encoding="utf-8") as handle:
                    return json.load(handle)
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Unreadable EDGAR cache %s: %s", cache_path, exc)

        for attempt in range(1, 4):
            self._throttle()
            try:
                response = self._session.get(url, timeout=self._timeout)
            except requests.RequestException as exc:
                logger.debug("EDGAR request failed for %s: %s", url, exc)
                return None

            if response.status_code in (429, 403):
                backoff = 5.0 * attempt
                logger.info(
                    "EDGAR throttled (%d) on %s; waiting %.0fs",
                    response.status_code,
                    url,
                    backoff,
                )
                time.sleep(backoff)
                self._min_interval = min(self._min_interval * 1.5, 1.0)
                continue
            if response.status_code == 404:
                return None
            if not response.ok:
                logger.debug("EDGAR %s returned %d", url, response.status_code)
                return None

            try:
                payload = response.json()
            except ValueError:
                logger.debug("EDGAR %s returned non-JSON", url)
                return None

            if cache_path is not None:
                try:
                    with open(cache_path, "w", encoding="utf-8") as handle:
                        json.dump(payload, handle)
                except OSError as exc:
                    logger.warning("Could not cache EDGAR payload: %s", exc)
            return payload
        return None

    def ticker_cik_map(self) -> Dict[str, int]:
        """Uppercase ticker -> CIK, cached next to the raw company facts."""
        cache_path = self._raw_dir / "company_tickers.json"
        payload = self._get_json(TICKERS_URL, cache_path=cache_path)
        if payload is None:
            return {}
        mapping: Dict[str, int] = {}
        for entry in payload.values():
            ticker = str(entry.get("ticker") or "").upper()
            cik = entry.get("cik_str")
            if ticker and cik is not None:
                mapping[ticker] = int(cik)
        return mapping

    def company_facts(self, cik: int) -> Optional[dict]:
        padded = f"{cik:010d}"
        cache_path = self._raw_dir / f"CIK{padded}.json"
        return self._get_json(
            COMPANY_FACTS_URL.format(cik=padded), cache_path=cache_path
        )

    def close(self) -> None:
        self._session.close()


@dataclass
class TickerResult:
    ticker: str
    cik: Optional[int] = None
    rows: int = 0
    deduced_rows: int = 0
    filed_coverage_pct: float = 0.0
    first_period: Optional[str] = None
    last_period: Optional[str] = None
    tags_used: Dict[str, str] = field(default_factory=dict)
    error: str = ""


def fetch_us_fundamentals(
    tickers: List[str],
    raw_dir: Path,
    quarterly_dir: Path,
    client: Optional[EdgarClient] = None,
    fallback_ciks: Optional[Dict[str, int]] = None,
    resume: bool = True,
) -> List[TickerResult]:
    """Fetch, parse and cache the quarterly table for each ticker.

    ``fallback_ciks`` lets the caller supply CIKs from the universe CSV when
    the SEC ticker map misses a name (share-class tickers mostly). Raw JSON is
    cached under ``raw_dir``; already-cached tickers are re-parsed but not
    re-downloaded, and tickers with an up-to-date output CSV are skipped
    entirely when ``resume`` is set.
    """
    quarterly_dir = Path(quarterly_dir)
    quarterly_dir.mkdir(parents=True, exist_ok=True)
    own_client = client is None
    client = client or EdgarClient(raw_dir)

    try:
        ticker_map = client.ticker_cik_map()
        if not ticker_map:
            logger.warning("SEC ticker->CIK map unavailable; relying on fallback CIKs")

        results: List[TickerResult] = []
        for index, raw_ticker in enumerate(tickers, start=1):
            ticker = raw_ticker.upper().strip()
            out_path = quarterly_dir / f"{ticker.replace('.', '_')}.csv"
            if resume and out_path.exists():
                try:
                    cached = pd.read_csv(out_path)
                    results.append(
                        TickerResult(
                            ticker=ticker,
                            rows=len(cached),
                            deduced_rows=int(cached["frame_deduced"].sum())
                            if "frame_deduced" in cached
                            else 0,
                            filed_coverage_pct=100.0 * cached["filed"].notna().mean()
                            if len(cached)
                            else 0.0,
                            first_period=str(cached["period_end"].iloc[0])
                            if len(cached)
                            else None,
                            last_period=str(cached["period_end"].iloc[-1])
                            if len(cached)
                            else None,
                        )
                    )
                except (pd.errors.ParserError, KeyError, OSError):
                    results.append(TickerResult(ticker=ticker, error="cached unreadable"))
                continue

            result = TickerResult(ticker=ticker)
            cik = ticker_map.get(ticker) or ticker_map.get(ticker.replace(".", "-"))
            fallback_cik = (fallback_ciks or {}).get(ticker)
            if cik is None:
                cik = fallback_cik
            if cik is None:
                result.error = "no CIK mapping"
                results.append(result)
                continue

            # SEC's ticker map is not always right (XOM famously resolves to a
            # holding entity, not Exxon Mobil Corp). Try the mapped CIK first,
            # then the universe-file CIK when they disagree.
            candidates = [cik]
            if fallback_cik is not None and fallback_cik != cik:
                logger.debug(
                    "%s: SEC map CIK %s disagrees with universe CIK %s; trying both",
                    ticker,
                    cik,
                    fallback_cik,
                )
                candidates.append(fallback_cik)

            frame = pd.DataFrame()
            tags_used: Dict[str, str] = {}
            used_cik = candidates[0]
            fetch_failed = False
            for candidate in candidates:
                payload = client.company_facts(candidate)
                if payload is None:
                    fetch_failed = True
                    continue
                frame, tags_used = extract_quarterly(payload)
                used_cik = candidate
                if not frame.empty:
                    break
            result.cik = used_cik
            result.tags_used = tags_used

            if frame.empty:
                result.error = (
                    "companyfacts fetch failed"
                    if fetch_failed
                    else "no quarterly facts parsed"
                )
                results.append(result)
                continue

            frame.to_csv(out_path, index=False)
            result.rows = len(frame)
            result.deduced_rows = int(frame["frame_deduced"].sum())
            result.filed_coverage_pct = 100.0 * frame["filed"].notna().mean()
            result.first_period = str(frame["period_end"].iloc[0])
            result.last_period = str(frame["period_end"].iloc[-1])
            results.append(result)

            if index % 25 == 0 or index == len(tickers):
                logger.info("EDGAR progress: %d/%d tickers", index, len(tickers))
    finally:
        if own_client:
            client.close()

    return results
