"""Parser tests for the backtest data layer.

Everything here runs offline against saved fixtures: a trimmed SEC
companyfacts JSON and a Screener.in #quarters page, both under
``tests/fixtures/``. The fixtures are shaped to exercise the awkward cases —
YTD-to-quarter deduction, amended filings, instant facts, and announcement
blocks that may or may not carry results dates.
"""

from __future__ import annotations

import json
import pickle
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from src.backtest.edgar import extract_quarterly, standalone_quarters, DurationFact
from src.backtest.prices import (
    BacktestPriceStore,
    india_yahoo_symbol,
    us_yahoo_symbol,
)
from src.backtest.screener_quarters import (
    parse_announcements_html,
    parse_quarters_html,
    quarter_label_to_period_end,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load_companyfacts() -> dict:
    with open(FIXTURES / "companyfacts_sample.json", "r", encoding="utf-8") as handle:
        return json.load(handle)


def load_screener_html() -> str:
    with open(FIXTURES / "screener_quarters_sample.html", "r", encoding="utf-8") as handle:
        return handle.read()


def quarters_by_end(frame: pd.DataFrame) -> dict:
    return {str(row.period_end.date()): row for row in frame.itertuples()}


# ----------------------------------------------------------------------
# EDGAR extraction
# ----------------------------------------------------------------------
def test_edgar_extracts_direct_quarters_with_filing_dates():
    frame, tags = extract_quarterly(load_companyfacts())
    rows = quarters_by_end(frame)

    assert set(rows) == {
        "2017-03-31",
        "2023-03-31",
        "2023-06-30",
        "2023-09-30",
        "2023-12-31",
    }
    q1 = rows["2023-03-31"]
    assert q1.revenue == 1000
    assert str(q1.filed.date()) == "2023-05-02"
    assert not q1.frame_deduced
    assert q1.source_form == "10-Q"
    assert tags["revenue"] == (
        "RevenueFromContractWithCustomerExcludingAssessedTax+Revenues"
    )


def test_edgar_legacy_tag_fills_periods_the_primary_tag_misses():
    frame, _ = extract_quarterly(load_companyfacts())
    old = quarters_by_end(frame)["2017-03-31"]
    # 2017 Q1 only exists under the legacy "Revenues" tag.
    assert old.revenue == 800
    assert str(old.filed.date()) == "2017-04-27"
    assert not old.frame_deduced
    assert pd.isna(old.net_income)


def test_edgar_amended_filing_does_not_replace_first_filing():
    frame, _ = extract_quarterly(load_companyfacts())
    q1 = quarters_by_end(frame)["2023-03-31"]
    # The 10-Q/A refiled 1050 on 2023-05-20; the original 1000 filed 2023-05-02 wins.
    assert q1.revenue == 1000
    assert str(q1.filed.date()) == "2023-05-02"


def test_edgar_ytd_quarters_are_deduced_not_double_counted():
    frame, _ = extract_quarterly(load_companyfacts())
    rows = quarters_by_end(frame)

    # Q2 has a direct three-month filing: used as-is, not from H1 - Q1.
    q2 = rows["2023-06-30"]
    assert q2.revenue == 1500
    assert not q2.frame_deduced

    # Q3 only exists as 9M YTD (3900) minus H1 (2500): 1400, deduced.
    q3 = rows["2023-09-30"]
    assert q3.revenue == 1400
    assert q3.frame_deduced
    # The deduced quarter became knowable only when the 9M column was filed.
    assert str(q3.filed.date()) == "2023-11-03"


def test_edgar_q4_comes_from_the_annual_filing():
    frame, _ = extract_quarterly(load_companyfacts())
    q4 = quarters_by_end(frame)["2023-12-31"]
    assert q4.revenue == 5500 - 3900
    assert q4.frame_deduced
    assert q4.source_form == "10-K"
    assert str(q4.filed.date()) == "2024-02-09"
    # Net income has no Q4 fact at all: the cell is missing, not invented.
    assert pd.isna(q4.net_income)


def test_edgar_instant_facts_and_non_report_forms_are_skipped():
    frame, _ = extract_quarterly(load_companyfacts())
    # The instant fact (no start date, val 777) and the 8-K (val 9999) must
    # never leak into any quarter.
    assert 777 not in set(frame["revenue"])
    assert 9999 not in set(frame["revenue"])
    assert frame["revenue"].tolist() == [800, 1000, 1500, 1400, 1600]


def test_edgar_filing_dates_are_never_before_period_ends():
    frame, _ = extract_quarterly(load_companyfacts())
    assert (frame["filed"] >= frame["period_end"]).all()


def test_edgar_missing_metric_keeps_its_column():
    frame, tags = extract_quarterly(load_companyfacts())
    assert "operating_income" in frame.columns
    assert frame["operating_income"].isna().all()
    assert "operating_income" not in tags


def test_edgar_empty_payload_returns_empty_frame():
    frame, tags = extract_quarterly({"facts": {"us-gaap": {}}})
    assert frame.empty
    assert list(frame.columns) == [
        "period_end",
        "filed",
        "revenue",
        "net_income",
        "eps_diluted",
        "operating_income",
        "source_form",
        "frame_deduced",
    ]
    assert tags == {}


def test_standalone_quarters_prefers_direct_over_deduced():
    q1 = DurationFact(date(2023, 1, 1), date(2023, 3, 31), 100.0, date(2023, 5, 1), "10-Q")
    h1 = DurationFact(date(2023, 1, 1), date(2023, 6, 30), 250.0, date(2023, 8, 1), "10-Q")
    q2_direct = DurationFact(date(2023, 4, 1), date(2023, 6, 30), 150.0, date(2023, 8, 1), "10-Q")

    out = {fact.end: (fact, deduced) for fact, deduced in standalone_quarters([q1, h1, q2_direct])}
    assert out[date(2023, 6, 30)][0].value == 150.0
    assert out[date(2023, 6, 30)][1] is False


# ----------------------------------------------------------------------
# Screener quarterly parsing
# ----------------------------------------------------------------------
def test_quarter_label_to_period_end():
    assert quarter_label_to_period_end("Jun 2025") == date(2025, 6, 30)
    assert quarter_label_to_period_end("Mar 2025") == date(2025, 3, 31)
    assert quarter_label_to_period_end("Dec 2024") == date(2024, 12, 31)
    assert quarter_label_to_period_end("not a quarter") is None


def test_screener_quarters_parse_into_tidy_rows():
    frame = parse_quarters_html(load_screener_html())
    assert len(frame) == 5
    assert frame["quarter_label"].tolist() == [
        "Mar 2024",
        "Jun 2024",
        "Sep 2024",
        "Dec 2024",
        "Mar 2025",
    ]
    assert frame["period_end"].tolist()[-1] == date(2025, 3, 31)

    latest = frame.iloc[-1]
    assert latest.sales_cr == 14000
    assert latest.operating_profit_cr == 2800
    assert latest.opm_pct == 20
    assert latest.net_profit_cr == 2100
    assert latest.eps_rs == 17.26


def test_screener_quarters_handles_missing_section():
    frame = parse_quarters_html("<html><body><p>nothing here</p></body></html>")
    assert frame.empty
    assert list(frame.columns) == [
        "quarter_label",
        "period_end",
        "sales_cr",
        "operating_profit_cr",
        "opm_pct",
        "net_profit_cr",
        "eps_rs",
    ]


def test_screener_announcements_capture_results_dates():
    announcements = parse_announcements_html(load_screener_html())
    assert len(announcements) == 3

    first, second, third = announcements
    assert first["results_related"] is True
    assert first["title"] == (
        "Audited Financial Results For The Quarter And Year Ended March 31, 2025"
    )
    # "20 May" carries no year on the page; the parser infers the most recent
    # year for which that date is not in the future.
    today = date.today()
    expected_year = today.year if date(today.year, 5, 20) <= today else today.year - 1
    assert first["date"] == date(expected_year, 5, 20).isoformat()

    assert second["results_related"] is False
    assert second["title"] == "Postal Ballot Notice"
    expected_year2 = today.year if date(today.year, 4, 15) <= today else today.year - 1
    assert second["date"] == date(expected_year2, 4, 15).isoformat()

    # Relative markers: "2d" means two days before fetch day.
    assert third["results_related"] is True
    assert third["date"] == (today - timedelta(days=2)).isoformat()
    assert all(a["url"].startswith("https://www.bseindia.com/") for a in announcements)


def test_screener_announcements_absent_is_normal():
    assert parse_announcements_html("<html><body></body></html>") == []


# ----------------------------------------------------------------------
# Price store conventions (no network)
# ----------------------------------------------------------------------
def test_yahoo_symbol_conventions():
    assert us_yahoo_symbol("aapl") == "AAPL"
    assert us_yahoo_symbol("BRK.B") == "BRK-B"
    assert india_yahoo_symbol("reliance") == "RELIANCE.NS"
    assert india_yahoo_symbol("^CRSLDX") == "^CRSLDX"


def test_price_store_roundtrip_and_turnover(tmp_path):
    store = BacktestPriceStore(tmp_path / "prices")
    index = pd.date_range("2024-01-01", periods=100, freq="B")
    frame = pd.DataFrame(
        {"Close": range(100, 200), "Volume": [1000] * 100}, index=index
    )
    with open(tmp_path / "prices" / "TEST.pkl", "wb") as handle:
        pickle.dump(frame, handle)

    loaded = store.load("TEST")
    assert loaded is not None and len(loaded) == 100
    # mean(close * volume) over the last 60 sessions
    tail = frame.tail(60)
    expected = float((tail["Close"] * tail["Volume"]).mean())
    assert store.average_daily_turnover("TEST", window=60) == expected
    assert store.load("MISSING") is None
