"""Targeted news search for portfolio holdings.

Broad RSS feeds are tuned for precision, which means a quiet story about a
holding can slip through untagged. Because the fund cares far more about news
on names it owns, each holding also gets a direct Google News query. That gives
high recall exactly where it matters, without loosening the broad tagger.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote_plus

from src.config import Config, get_config
from src.logging_setup import get_logger
from src.models import NewsArticle
from src.news.ingest import SymbolTagger
from src.universe import load_universe

logger = get_logger(__name__)

GOOGLE_NEWS_RSS = (
    "https://news.google.com/rss/search?q={query}&hl=en-IN&gl=IN&ceid=IN:en"
)
_REQUEST_DELAY = 0.6


class HoldingsNewsFetcher:
    """Fetches recent news for a specific list of symbols."""

    def __init__(self, config: Optional[Config] = None):
        self._config = config or get_config()
        self._universe = load_universe(self._config)
        self._tagger = SymbolTagger(self._universe)
        self._keywords = [
            str(k).lower()
            for k in (self._config.get("news.high_impact_keywords", []) or [])
        ]

    # Below this length a company name is too generic to search on its own:
    # querying "BSE" returns every story that mentions the exchange.
    _MIN_DISTINCTIVE_NAME = 12

    def _names_for(self, symbol: str) -> Tuple[str, str]:
        """The company's full name and its name without the legal suffix."""
        entry = self._universe.get(symbol)
        full = (entry.company_name if entry else symbol).strip()
        short = re.sub(
            r"\s*(Ltd\.?|Limited|Pvt\.?|Private)\s*$", "", full
        ).strip() or full
        return full, short

    def _query_for(self, symbol: str) -> str:
        full, short = self._names_for(symbol)
        # Normally the journalist's form of the name is the best query. When that
        # is too short to be distinctive, keep the legal suffix, which is what
        # separates "BSE Ltd" the company from the BSE the venue.
        phrase = short if len(short) >= self._MIN_DISTINCTIVE_NAME else full
        return quote_plus(f'"{phrase}" (share OR stock OR results OR NSE)')

    def _mentions(self, text: str, symbol: str) -> bool:
        """True when the article actually names the company.

        Google News matches loosely, so a query can come back with a market
        round-up that merely sits near the search terms. The broad tagger already
        knows how the press writes each company name, so reuse it here rather
        than keep a second, weaker notion of what counts as a mention.
        """
        return symbol in self._tagger.tag(text)

    def fetch_for(
        self,
        symbols: Iterable[str],
        lookback_hours: Optional[int] = None,
        max_per_symbol: int = 5,
    ) -> Dict[str, List[NewsArticle]]:
        import feedparser

        lookback_hours = lookback_hours or int(
            self._config.get("news.lookback_hours", 36) or 36
        )
        cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
        symbols = [s.upper() for s in dict.fromkeys(symbols)]

        out: Dict[str, List[NewsArticle]] = {}
        if not symbols:
            return out

        logger.info("Fetching targeted news for %d holding(s)", len(symbols))

        for symbol in symbols:
            url = GOOGLE_NEWS_RSS.format(query=self._query_for(symbol))
            try:
                parsed = feedparser.parse(url)
            except Exception as exc:
                logger.debug("Targeted news failed for %s: %s", symbol, exc)
                continue
            time.sleep(_REQUEST_DELAY)

            articles: List[NewsArticle] = []
            for entry in list(parsed.entries or [])[: max_per_symbol * 3]:
                title = " ".join(str(getattr(entry, "title", "")).split())
                if not title:
                    continue

                published: Optional[datetime] = None
                for attribute in ("published_parsed", "updated_parsed"):
                    stamp = getattr(entry, attribute, None)
                    if stamp:
                        try:
                            published = datetime(*stamp[:6], tzinfo=timezone.utc)
                            break
                        except (TypeError, ValueError):
                            continue

                if published is not None and published < cutoff:
                    continue

                summary = re.sub(
                    r"<[^>]+>", "", " ".join(str(getattr(entry, "summary", "")).split())
                )[:400]

                if not self._mentions(f"{title} {summary}", symbol):
                    logger.debug("Dropped off-topic %s result: %s", symbol, title)
                    continue

                lowered = f"{title} {summary}".lower()
                matched = [k for k in self._keywords if k in lowered]

                articles.append(
                    NewsArticle(
                        title=title,
                        url=str(getattr(entry, "link", "")),
                        source=f"Google News ({symbol})",
                        published=published,
                        summary=summary,
                        symbols=[symbol],
                        is_high_impact=bool(matched),
                        matched_keywords=matched,
                    )
                )
                if len(articles) >= max_per_symbol:
                    break

            if articles:
                out[symbol] = articles

        logger.info(
            "Targeted news: %d/%d holdings had recent coverage",
            len(out),
            len(symbols),
        )
        return out
