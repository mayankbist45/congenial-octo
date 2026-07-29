"""RSS news ingestion and relevance tagging.

Collects headlines from Indian financial feeds, deduplicates syndicated
coverage, and tags each article with the universe symbols it mentions. Tagging
is deliberately conservative: a false positive would send a spurious
company-specific alert into the decision layer.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Set, Tuple

from src.config import Config, get_config
from src.logging_setup import get_logger
from src.models import NewsArticle
from src.universe import Universe, load_universe

logger = get_logger(__name__)

# Legal-form words stripped from the tail of a company name. Deliberately
# narrow: words like "Bank", "Finance" or "Industries" are part of a company's
# identity ("Federal Bank" is not "Federal") and must be kept.
_LEGAL_SUFFIXES = {
    "ltd",
    "limited",
    "plc",
    "inc",
    "incorporated",
    "pvt",
    "private",
    "co",
    "the",
}

# Common English words that also happen to be NSE tickers. Matching these
# case-sensitively is not enough, because headlines capitalise words in titles.
_ENGLISH_WORD_TICKERS = {
    "ALL",
    "ANT",
    "ARE",
    "ART",
    "BABA",
    "BLS",
    "CAN",
    "CARE",
    "CLEAN",
    "CUB",
    "FACT",
    "FINE",
    "FIVESTAR",
    "FOR",
    "GET",
    "GOLD",
    "GRAVITA",
    "HAS",
    "IDEA",
    "JET",
    "LEMON",
    "MAP",
    "MAX",
    "NET",
    "NOW",
    "OIL",
    "ONE",
    "OWN",
    "PAY",
    "PIX",
    "POWER",
    "RUN",
    "SAIL",
    "SET",
    "SHARE",
    "SIS",
    "STAR",
    "SUN",
    "SWAN",
    "TIP",
    "TOP",
    "TRENT",
    "TWO",
    "USE",
    "WIN",
    "ZEN",
}


# Short tickers collide with acronyms constantly in Indian financial media, so
# a 3-letter match must be corroborated by equity context somewhere in the text.
_EQUITY_CONTEXT = re.compile(
    r"\b(shares?|stock|stocks|scrip|equity|equities|nse|bse|nifty|sensex"
    r"|q[1-4]|results|profit|revenue|earnings|ebitda|margin|guidance|dividend"
    r"|target price|upgrade|downgrade|rating|buy|sell|hold|brokerage"
    r"|market cap|52-week|board meeting|acquisition|merger|stake)\b",
    re.IGNORECASE,
)

# Phrases that mean a ticker-looking acronym is being used in another sense.
# "ITC" is Input Tax Credit far more often than it is ITC Ltd in tax coverage.
_TICKER_FALSE_FRIENDS = {
    "ITC": re.compile(r"\b(input tax credit|tax credit|gst)\b", re.IGNORECASE),
    "PNB": re.compile(r"\bpunjab national bank fraud case\b", re.IGNORECASE),
}


# A few listed companies are themselves the venue or index that the press names
# in passing. "BSE Sensex rallies" and "gold on MCX" are market commentary, not
# news about BSE Ltd or MCX Ltd, and market copy is saturated with that usage.
# Blacklisting venue phrases is not enough, because these tickers also appear as
# bare exchange-data noise. So for these names the tagger demands the opposite:
# positive evidence that the story is about the company itself.
_VENUE_TICKERS = ("BSE", "MCX", "IEX", "NSE")

# Wordings that put the ticker in a corporate frame rather than a venue one.
_COMPANY_FRAME_TEMPLATES = (
    r"\b{t}\s*(?:Ltd|Limited)\b",
    r"\b{t}'s\b",
    r"\b{t}\s+(?:share|shares|stock|scrip|share\s+price|shareholders?)\b",
    r"\bshares?\s+of\s+{t}\b",
    r"\b{t}\s+Q[1-4]\b",
    r"\b{t}\s+(?:FY|H[12])\d*\b",
    r"\b{t}\s+(?:results|earnings|profit|PAT|revenue|income|margin|dividend"
    r"|bonus|buyback|split|board|management|MD|CEO|stake|acquisition|merger"
    r"|guidance|outlook|order\s+book|market\s+cap|valuation|target\s+price"
    r"|rating|upgrade|downgrade)\b",
    r"\b{t}\s+(?:reports?|posts?|logs?|announces?|declares?|beats?|misses?"
    r"|approves?|appoints?|schedules?|files?|launches?|wins?|bags?)\b",
    # Price action written about the company as a subject.
    r"\b{t}\s+(?:hits?|jumps?|surges?|rallies|rises?|gains?|climbs?|soars?"
    r"|falls?|drops?|slips?|slides?|declines?|tanks?|plunges?|tumbles?"
    r"|ends?|closes?|trades?)\b",
    r"\b(?:buy|sell|hold|accumulate)\s+{t}\b",
)

_TICKER_COMPANY_USAGE = {
    ticker: re.compile(
        "|".join(t.format(t=re.escape(ticker)) for t in _COMPANY_FRAME_TEMPLATES),
        re.IGNORECASE,
    )
    for ticker in _VENUE_TICKERS
}


def _tokenise(text: str) -> List[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def _strip_legal_suffix(company_name: str) -> List[str]:
    """Company name tokens with legal-form words removed."""
    tokens = _tokenise(company_name)
    while tokens and tokens[-1] in _LEGAL_SUFFIXES:
        tokens.pop()
    while tokens and tokens[0] in _LEGAL_SUFFIXES:
        tokens.pop(0)
    return tokens


_TRUNCATION_STEM = 6
# Company names are written with varying connectors: "Larsen & Toubro",
# "Larsen and Toubro", "Bajaj-Auto", "L.T.".
_SEPARATOR = r"(?:[\s\-\.,&/]|\band\b)+"


def _build_pattern(tokens: List[str], allow_truncation: bool) -> re.Pattern:
    """Compile an alias phrase into a regex.

    When truncation is allowed, the final token also matches the abbreviations
    the press actually uses: ``sun pharmaceutical`` then matches "Sun Pharma".
    Callers validate that the looser pattern still identifies one company
    before keeping it.
    """
    parts = [re.escape(t) for t in tokens[:-1]]
    tail = tokens[-1]
    if allow_truncation and len(tail) > _TRUNCATION_STEM:
        parts.append(f"{re.escape(tail[:_TRUNCATION_STEM])}\\w*")
    else:
        parts.append(re.escape(tail))
    return re.compile(r"\b" + _SEPARATOR.join(parts) + r"\b", re.IGNORECASE)


class SymbolTagger:
    """Maps free text to universe symbols.

    Two independent signals are used, both tuned for precision because a false
    company tag would push a spurious alert into the decision layer:

    1. The ticker appearing verbatim in upper case ("TCS Q1 results"). Tickers
       that are also English words are excluded, since headline title-casing
       makes them indistinguishable from ordinary prose.
    2. A company-name alias phrase. An alias is the shortest token prefix of
       the name that is unique across the whole universe, and a single-word
       alias is only allowed when the company's name really is one word. That
       is what lets "HDFC Bank" tag HDFCBANK rather than HDFC Life or HDFC AMC,
       while "Federal Reserve" tags nothing because Federal Bank's alias is the
       two-word phrase.

    Every compiled pattern is validated against all 500 company names, so a
    loose pattern that would collide with another listed company is rejected.
    """

    def __init__(self, universe: Universe):
        self._universe = universe
        self._aliases: Dict[str, re.Pattern] = {}
        self._ticker_patterns: Dict[str, re.Pattern] = {}

        stripped: Dict[str, List[str]] = {}
        prefix_owners: Dict[Tuple[str, ...], Set[str]] = {}

        for entry in universe:
            tokens = _strip_legal_suffix(entry.company_name)
            if not tokens:
                continue
            stripped[entry.symbol] = tokens
            for length in range(1, len(tokens) + 1):
                prefix_owners.setdefault(tuple(tokens[:length]), set()).add(entry.symbol)

        names = {symbol: " ".join(tokens) for symbol, tokens in stripped.items()}

        for symbol, tokens in stripped.items():
            alias = self._choose_alias(symbol, tokens, prefix_owners, names)
            if alias is not None:
                self._aliases[symbol] = alias

        for entry in universe:
            symbol = entry.symbol
            if len(symbol) < 3 or symbol in _ENGLISH_WORD_TICKERS:
                continue
            self._ticker_patterns[symbol] = re.compile(rf"\b{re.escape(symbol)}\b")

        logger.debug(
            "Tagger built: %d name aliases, %d ticker patterns",
            len(self._aliases),
            len(self._ticker_patterns),
        )

    def _choose_alias(
        self,
        symbol: str,
        tokens: List[str],
        prefix_owners: Dict[Tuple[str, ...], Set[str]],
        names: Dict[str, str],
    ) -> Optional[re.Pattern]:
        for length in range(1, len(tokens) + 1):
            prefix = tuple(tokens[:length])
            owners = prefix_owners.get(prefix, set())

            if owners != {symbol}:
                # Shared prefix. The one exception: when this company's full
                # name is exactly the shared prefix, it is the parent entity
                # (e.g. "Tata Motors" vs "Tata Motors Passenger Vehicles"), so
                # the bare phrase should resolve to the parent.
                if not (length == len(tokens) and symbol in owners):
                    continue

            if length == 1 and (len(tokens) > 1 or len(prefix[0]) < 5):
                continue

            # Prefer the tolerant pattern, but only if it stays unambiguous.
            for allow_truncation in (True, False):
                pattern = _build_pattern(list(prefix), allow_truncation)
                hits = [s for s, name in names.items() if pattern.search(name)]
                if hits == [symbol] or (len(hits) > 1 and set(hits) == owners and symbol in hits and length == len(tokens)):
                    return pattern
            return None
        return None

    def tag(self, text: str) -> List[str]:
        """Return universe symbols confidently referenced by ``text``."""
        if not text:
            return []

        matches: Set[str] = set()
        has_equity_context = bool(_EQUITY_CONTEXT.search(text))

        # Signal 1: verbatim upper-case ticker.
        for symbol, pattern in self._ticker_patterns.items():
            if not pattern.search(text):
                continue
            if len(symbol) <= 3 and not has_equity_context:
                continue
            false_friend = _TICKER_FALSE_FRIENDS.get(symbol)
            if false_friend is not None and false_friend.search(text):
                continue
            company_frame = _TICKER_COMPANY_USAGE.get(symbol)
            if company_frame is not None and not company_frame.search(text):
                # The ticker names a venue or index unless the story frames it
                # as a company.
                continue
            matches.add(symbol)

        # Signal 2: unique company-name alias phrase.
        for symbol, pattern in self._aliases.items():
            if symbol in matches:
                continue
            if pattern.search(text):
                matches.add(symbol)

        return sorted(matches)


class NewsIngestor:
    """Fetches configured RSS feeds and returns tagged, deduplicated articles."""

    def __init__(self, config: Optional[Config] = None):
        self._config = config or get_config()
        self._universe = load_universe(self._config)
        self._tagger = SymbolTagger(self._universe)
        self._keywords = [
            str(k).lower()
            for k in (self._config.get("news.high_impact_keywords", []) or [])
        ]

    def _parse_published(self, entry: object) -> Optional[datetime]:
        for attribute in ("published_parsed", "updated_parsed"):
            parsed = getattr(entry, attribute, None)
            if parsed:
                try:
                    return datetime(*parsed[:6], tzinfo=timezone.utc)
                except (TypeError, ValueError):
                    continue
        return None

    def _classify_impact(self, text: str) -> Tuple[bool, List[str]]:
        lowered = text.lower()
        matched = [k for k in self._keywords if k in lowered]
        return bool(matched), matched

    def fetch(self, lookback_hours: Optional[int] = None) -> List[NewsArticle]:
        import feedparser

        lookback_hours = lookback_hours or int(
            self._config.get("news.lookback_hours", 36) or 36
        )
        max_per_feed = int(self._config.get("news.max_articles_per_feed", 60) or 60)
        cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)

        feeds = self._config.get("news.feeds", []) or []
        articles: List[NewsArticle] = []
        seen: Set[str] = set()

        for feed in feeds:
            name = str(feed.get("name") or "unknown")
            url = str(feed.get("url") or "")
            if not url:
                continue

            try:
                parsed = feedparser.parse(url)
            except Exception as exc:
                logger.warning("Feed %s failed: %s", name, exc)
                continue

            entries = list(parsed.entries or [])[:max_per_feed]
            if not entries:
                logger.warning("Feed %s returned no entries", name)
                continue

            kept = 0
            for entry in entries:
                title = " ".join(str(getattr(entry, "title", "")).split())
                if not title:
                    continue

                published = self._parse_published(entry)
                # Feeds with no timestamps are kept: dropping them would lose
                # coverage, and the digest is capped by relevance anyway.
                if published is not None and published < cutoff:
                    continue

                summary = " ".join(str(getattr(entry, "summary", "")).split())
                # Strip any HTML the feed embedded in its summary.
                summary = re.sub(r"<[^>]+>", "", summary)[:600]

                article = NewsArticle(
                    title=title,
                    url=str(getattr(entry, "link", "")),
                    source=name,
                    published=published,
                    summary=summary,
                )

                if article.fingerprint in seen:
                    continue
                seen.add(article.fingerprint)

                searchable = f"{title} {summary}"
                article.symbols = self._tagger.tag(searchable)
                article.is_high_impact, article.matched_keywords = self._classify_impact(
                    searchable
                )

                articles.append(article)
                kept += 1

            logger.info("Feed %-30s %d/%d articles kept", name, kept, len(entries))

        articles.sort(
            key=lambda a: a.published or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        tagged = sum(1 for a in articles if a.symbols)
        high_impact = sum(1 for a in articles if a.is_high_impact)
        logger.info(
            "Ingested %d unique articles (%d symbol-tagged, %d high impact)",
            len(articles),
            tagged,
            high_impact,
        )
        return articles

    def articles_for(
        self, articles: Iterable[NewsArticle], symbols: Iterable[str]
    ) -> Dict[str, List[NewsArticle]]:
        """Group articles by the symbols of interest."""
        wanted = {s.upper() for s in symbols}
        out: Dict[str, List[NewsArticle]] = {}
        for article in articles:
            for symbol in article.symbols:
                if symbol in wanted:
                    out.setdefault(symbol, []).append(article)
        return out
