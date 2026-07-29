"""Targeted holding-news precision tests.

The per-holding Google News search is deliberately high-recall, which makes it
the easiest place for an unrelated story to enter the briefing. These tests pin
down the two guards: a query distinctive enough to identify the company, and a
check that the returned article actually names it.
"""

from __future__ import annotations

from urllib.parse import unquote_plus

import pytest

from src.news.holdings_news import HoldingsNewsFetcher


@pytest.fixture(scope="module")
def fetcher() -> HoldingsNewsFetcher:
    return HoldingsNewsFetcher()


def query_text(fetcher: HoldingsNewsFetcher, symbol: str) -> str:
    return unquote_plus(fetcher._query_for(symbol))


def test_short_names_keep_the_legal_suffix(fetcher: HoldingsNewsFetcher) -> None:
    """"BSE" alone matches every market story, so the query must say "BSE Ltd"."""
    query = query_text(fetcher, "BSE")
    assert '"BSE Ltd' in query


def test_distinctive_names_drop_the_legal_suffix(fetcher: HoldingsNewsFetcher) -> None:
    """A journalist writes "Tata Consultancy Services", not the registered name."""
    query = query_text(fetcher, "TCS")
    assert '"Tata Consultancy Services"' in query


def test_off_topic_results_are_rejected(fetcher: HoldingsNewsFetcher) -> None:
    """A market round-up returned for a BSE query is not news about BSE Ltd."""
    headline = (
        "Pidilite Industries among 7 commodity stocks to hit 52-week highs "
        "and surge up to 25% in a month"
    )
    assert not fetcher._mentions(headline, "BSE")


def test_on_topic_results_are_kept(fetcher: HoldingsNewsFetcher) -> None:
    assert fetcher._mentions("BSE Q1 profit doubles on derivatives volumes", "BSE")
    assert fetcher._mentions(
        "Tata Consultancy Services wins a large European deal", "TCS"
    )
    assert fetcher._mentions("TCS board approves a share buyback", "TCS")


def test_partial_name_still_counts_as_a_mention(fetcher: HoldingsNewsFetcher) -> None:
    """Headlines shorten long names; the leading words are enough to identify."""
    assert fetcher._mentions("Sun Pharma gets USFDA nod for a generic", "SUNPHARMA")
