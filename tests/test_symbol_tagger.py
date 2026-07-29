"""Symbol tagging precision tests.

A false company tag sends a spurious alert into the decision layer, so these
cases lock in the behaviour that matters: macro headlines must tag nothing, and
ambiguous corporate families must resolve to the right entity.
"""

from __future__ import annotations

import pytest

from src.news.ingest import SymbolTagger
from src.universe import load_universe


@pytest.fixture(scope="module")
def tagger() -> SymbolTagger:
    return SymbolTagger(load_universe())


# Macro and generic headlines that must not tag any company.
NO_MATCH_CASES = [
    "US Federal Reserve keeps rates steady as inflation risks keep markets on edge",
    "Bharat atta, rice set for market return ahead of festive season",
    "Gold prices cross 4100 dollars as Fed pauses",
    "Global markets slip on trade war fears",
    "Sensex and Nifty end flat amid choppy trade",
    "Crude oil rises on supply concerns in the Middle East",
    "RBI holds repo rate at 6.5% for the sixth straight meeting",
    "General insurance premiums grow across the industry",
    "New power capacity additions hit a record this year",
]

# Headlines that must resolve to exactly one specific company.
EXACT_MATCH_CASES = [
    ("TCS Q1 results beat estimates, board approves dividend", "TCS"),
    ("Reliance Industries to invest in new energy business", "RELIANCE"),
    ("Asian Paints reports margin expansion in Q1", "ASIANPAINT"),
    ("HDFC Bank net profit rises 12% on strong loan growth", "HDFCBANK"),
    ("HDFC Life Insurance premium income grows", "HDFCLIFE"),
    ("Infosys wins large deal from European retailer", "INFY"),
    ("Sun Pharma gets USFDA nod for generic drug", "SUNPHARMA"),
    ("Federal Bank reports higher NIM in Q1", "FEDERALBNK"),
    ("Bajaj Finance shares fall on asset quality concerns", "BAJFINANCE"),
    ("IndusInd Bank discloses accounting lapse in derivatives book", "INDUSINDBK"),
    ("Hexaware Technologies Q1 profit slips 6%", "HEXT"),
    ("Larsen & Toubro bags order for metro project", "LT"),
]


@pytest.mark.parametrize("headline", NO_MATCH_CASES)
def test_macro_headlines_tag_nothing(tagger: SymbolTagger, headline: str) -> None:
    assert tagger.tag(headline) == [], f"false positive on: {headline}"


@pytest.mark.parametrize("headline,expected", EXACT_MATCH_CASES)
def test_company_headlines_tag_the_right_company(
    tagger: SymbolTagger, headline: str, expected: str
) -> None:
    tags = tagger.tag(headline)
    assert expected in tags, f"missed {expected} in: {headline} (got {tags})"
    assert len(tags) == 1, f"over-tagged {headline}: {tags}"


def test_generic_word_tickers_are_not_matched_in_prose(tagger: SymbolTagger) -> None:
    """Tickers that are ordinary words must not fire on prose usage."""
    for headline in [
        "Clean energy investment reaches a new high",
        "Power demand peaks during the summer months",
        "Max temperature records broken across north India",
        "Trent in talks over new store format",
    ]:
        tags = tagger.tag(headline)
        assert "CLEAN" not in tags
        assert "POWER" not in tags
        assert "MAX" not in tags


def test_uppercase_ticker_is_recognised(tagger: SymbolTagger) -> None:
    assert "SUNPHARMA" in tagger.tag("SUNPHARMA hits 52-week high")
    assert "ITC" in tagger.tag("ITC shares rise after Q1 results beat estimates")


def test_short_tickers_require_equity_context(tagger: SymbolTagger) -> None:
    """Three-letter tickers collide with acronyms; prose alone must not tag."""
    assert tagger.tag("ITC fraud worth Rs 74,782 crore detected in FY26") == []
    assert tagger.tag("Input tax credit (ITC) claims under GST scrutiny") == []


def test_acronym_false_friends_are_suppressed(tagger: SymbolTagger) -> None:
    """Even with equity context, a clear alternative meaning wins."""
    headline = (
        "GST officers detect input tax credit ITC fraud; shares of several "
        "firms under scrutiny"
    )
    assert "ITC" not in tagger.tag(headline)


# Tickers that double as the name of a venue or index. Market round-ups mention
# these constantly and must not be read as news about the listed company.
VENUE_USAGE_CASES = [
    "Why did the stock market rise today? BSE Sensex rallies 1,000 points; Nifty50 crosses 24,250",
    "Stock market holiday: NSE, BSE, MCX trading status for Independence Day",
    "Shares of the company were listed on BSE at a premium to the issue price",
    "BSE Midcap and Smallcap indices outperform the benchmark",
    "MCX gold futures ease as bullion demand softens",
    # Bare exchange-data noise, which market copy is full of.
    "Pidilite Industries among 7 commodity stocks to hit 52-week highs; volumes on BSE, NSE surge",
    "These 15 midcap stocks soared up to 98% in a year (Source: BSE, NSE, Ace Equity)",
    "Silver hits a record on MCX while gold holds near its peak",
    "Power exchange volumes: IEX spot prices ease as supply improves",
]


@pytest.mark.parametrize("headline", VENUE_USAGE_CASES)
def test_exchange_tickers_are_not_tagged_on_venue_mentions(
    tagger: SymbolTagger, headline: str
) -> None:
    tags = tagger.tag(headline)
    assert "BSE" not in tags, f"false positive on: {headline}"
    assert "MCX" not in tags, f"false positive on: {headline}"


def test_exchange_companies_still_tag_on_their_own_news(tagger: SymbolTagger) -> None:
    """The suppression must not blind the fund to real news on these holdings."""
    assert "BSE" in tagger.tag("BSE Q1 profit doubles on higher derivatives volumes")
    assert "BSE" in tagger.tag("BSE shares hit a record high after the stock split")
    assert "MCX" in tagger.tag("MCX reports a jump in Q2 revenue and margin")


def test_venue_mention_does_not_mask_a_real_reference(tagger: SymbolTagger) -> None:
    """A story about the company that also names the venue still tags."""
    headline = (
        "BSE shares jump 5% on the NSE and BSE as the BSE Sensex holds flat; "
        "BSE Q1 revenue beats estimates"
    )
    assert "BSE" in tagger.tag(headline)
