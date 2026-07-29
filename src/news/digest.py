"""Daily news digest.

Turns a few hundred raw headlines into the briefing a portfolio manager would
actually read before the open: what moved macro, what moved the market, and
what specifically happened to the names the fund owns or is watching.

The LLM summarises when available. Without it, a keyword-based digest is
produced instead, so the daily cycle never depends on an external service.
"""

from __future__ import annotations

from datetime import date
from typing import Dict, Iterable, List, Optional, Sequence

from src.config import Config, get_config
from src.llm import LLMClient, LLMUnavailable
from src.logging_setup import get_logger
from src.models import NewsArticle, NewsDigest

logger = get_logger(__name__)

SYSTEM_PROMPT = """\
You are a senior research analyst at an Indian equity mutual fund. Each morning \
you brief the portfolio manager before the market opens.

Your job is to separate signal from noise. The fund holds Indian equities on a \
multi-month to multi-year horizon, so day-to-day market chatter matters far \
less than developments that change a company's earnings power, a sector's \
outlook, or the macro backdrop for Indian equities.

Be specific and factual. Cite numbers when the article gives them. Never \
speculate beyond what the headlines support, and never recommend a trade - the \
portfolio manager makes those decisions. If the news is quiet, say so plainly \
rather than manufacturing significance.

Respond with a single JSON object and nothing else."""

USER_TEMPLATE = """\
Today is {today}.

The fund currently HOLDS these positions:
{holdings}

The fund is WATCHING these candidates:
{watchlist}

Below are today's headlines from Indian financial media.

=== MACRO AND MARKET HEADLINES ===
{macro_articles}

=== NEWS MENTIONING HOLDINGS OR WATCHLIST NAMES ===
{company_articles}

Produce a JSON object with exactly these keys:

{{
  "macro_summary": "2-4 sentences on the macro backdrop for Indian equities: \
central bank policy, inflation, rates, currency, crude, global risk events. \
State 'Nothing materially new.' if there is nothing of substance.",
  "market_summary": "2-4 sentences on Indian equity market conditions and \
sector-level moves relevant to this portfolio.",
  "holdings_notes": {{
    "SYMBOL": "1-3 sentences on what happened to this holding and why it \
matters to the investment case. Only include symbols with genuine news."
  }},
  "watchlist_notes": {{
    "SYMBOL": "1-2 sentences on news relevant to a potential purchase. Only \
include symbols with genuine news."
  }},
  "flagged_symbols": ["SYMBOL", ...]
}}

Put a symbol in "flagged_symbols" only when the news suggests a possible \
change to the investment case that the portfolio manager must look at today - \
for example an earnings miss or cut guidance, a governance or fraud concern, a \
regulatory or legal action, a credit default or rating downgrade, the exit of \
a key executive, promoter stake sale or pledging, or a major acquisition. \
Routine price moves, analyst rating changes and broker target revisions are \
NOT grounds for flagging."""


def _format_article_lines(
    articles: Sequence[NewsArticle], limit: int, include_symbols: bool = False
) -> str:
    if not articles:
        return "(none)"
    lines: List[str] = []
    for article in articles[:limit]:
        stamp = article.published.strftime("%d %b %H:%M") if article.published else "—"
        prefix = f"[{','.join(article.symbols)}] " if include_symbols and article.symbols else ""
        line = f"- {prefix}{article.title} ({article.source}, {stamp})"
        if article.summary:
            line += f"\n    {article.summary[:240]}"
        lines.append(line)
    return "\n".join(lines)


class DigestBuilder:
    """Builds the daily news digest."""

    def __init__(self, config: Optional[Config] = None, llm: Optional[LLMClient] = None):
        self._config = config or get_config()
        self._llm = llm or LLMClient(self._config)

    def build(
        self,
        articles: List[NewsArticle],
        holdings: Iterable[str],
        watchlist: Iterable[str],
        holdings_articles: Optional[Dict[str, List[NewsArticle]]] = None,
        as_of: Optional[date] = None,
    ) -> NewsDigest:
        as_of = as_of or date.today()
        holdings = [s.upper() for s in holdings]
        watchlist = [s.upper() for s in watchlist]
        relevant = set(holdings) | set(watchlist)

        # Merge in targeted per-holding coverage.
        combined: List[NewsArticle] = list(articles)
        seen = {a.fingerprint for a in combined}
        for symbol_articles in (holdings_articles or {}).values():
            for article in symbol_articles:
                if article.fingerprint not in seen:
                    combined.append(article)
                    seen.add(article.fingerprint)

        company_articles = [
            a for a in combined if any(s in relevant for s in a.symbols)
        ]
        macro_articles = [
            a
            for a in combined
            if not any(s in relevant for s in a.symbols)
            and (a.is_high_impact or not a.symbols)
        ]

        digest = NewsDigest(
            as_of=as_of,
            article_count=len(combined),
            high_impact_count=sum(1 for a in combined if a.is_high_impact),
            articles=combined,
        )

        if self._llm.available:
            try:
                self._fill_from_llm(
                    digest, macro_articles, company_articles, holdings, watchlist, as_of
                )
                digest.generated_by_llm = True
                logger.info(
                    "LLM digest built: %d holding note(s), %d flagged",
                    len(digest.holdings_notes),
                    len(digest.flagged_symbols),
                )
                return digest
            except LLMUnavailable as exc:
                logger.warning("LLM digest failed (%s); using keyword digest", exc)

        self._fill_from_keywords(digest, macro_articles, company_articles, relevant)
        return digest

    def _fill_from_llm(
        self,
        digest: NewsDigest,
        macro_articles: List[NewsArticle],
        company_articles: List[NewsArticle],
        holdings: List[str],
        watchlist: List[str],
        as_of: date,
    ) -> None:
        max_articles = int(self._config.get("news.max_articles_in_digest", 40) or 40)

        prompt = USER_TEMPLATE.format(
            today=as_of.strftime("%A, %d %B %Y"),
            holdings=", ".join(holdings) if holdings else "(none - portfolio is empty)",
            watchlist=", ".join(watchlist[:40]) if watchlist else "(none)",
            macro_articles=_format_article_lines(macro_articles, max_articles),
            company_articles=_format_article_lines(
                company_articles, max_articles, include_symbols=True
            ),
        )

        payload = self._llm.complete_json(
            SYSTEM_PROMPT, prompt, model=self._llm.digest_model
        )
        if not isinstance(payload, dict):
            raise LLMUnavailable("Digest response was not a JSON object")

        digest.macro_summary = str(payload.get("macro_summary") or "").strip()
        digest.market_summary = str(payload.get("market_summary") or "").strip()

        known = set(holdings) | set(watchlist)
        holdings_notes = payload.get("holdings_notes") or {}
        if isinstance(holdings_notes, dict):
            digest.holdings_notes = {
                str(k).upper(): str(v).strip()
                for k, v in holdings_notes.items()
                if str(k).upper() in known and str(v).strip()
            }

        watchlist_notes = payload.get("watchlist_notes") or {}
        if isinstance(watchlist_notes, dict):
            digest.watchlist_notes = {
                str(k).upper(): str(v).strip()
                for k, v in watchlist_notes.items()
                if str(k).upper() in known and str(v).strip()
            }

        flagged = payload.get("flagged_symbols") or []
        if isinstance(flagged, list):
            digest.flagged_symbols = sorted(
                {str(s).upper() for s in flagged if str(s).upper() in known}
            )

    def _fill_from_keywords(
        self,
        digest: NewsDigest,
        macro_articles: List[NewsArticle],
        company_articles: List[NewsArticle],
        relevant: set,
    ) -> None:
        """Deterministic fallback digest."""
        macro_headlines = [a.title for a in macro_articles if a.is_high_impact][:8]
        digest.macro_summary = (
            "Keyword digest (no LLM configured). High-impact macro headlines: "
            + "; ".join(macro_headlines)
            if macro_headlines
            else "Keyword digest (no LLM configured). No high-impact macro headlines."
        )
        digest.market_summary = (
            f"{len(digest.articles)} articles ingested, "
            f"{digest.high_impact_count} matched high-impact keywords."
        )

        # Group headlines per symbol, and flag names whose coverage contains
        # words that historically precede a thesis break.
        severe = {
            "fraud",
            "resign",
            "default",
            "insolvency",
            "raid",
            "probe",
            "downgrade",
            "cut guidance",
            "profit warning",
        }
        by_symbol: Dict[str, List[str]] = {}
        flagged: set = set()
        for article in company_articles:
            for symbol in article.symbols:
                if symbol not in relevant:
                    continue
                by_symbol.setdefault(symbol, []).append(article.title)
                lowered = f"{article.title} {article.summary}".lower()
                if any(word in lowered for word in severe):
                    flagged.add(symbol)

        digest.holdings_notes = {
            symbol: " | ".join(titles[:3]) for symbol, titles in by_symbol.items()
        }
        digest.flagged_symbols = sorted(flagged)
        logger.info(
            "Keyword digest built: %d symbol note(s), %d flagged",
            len(digest.holdings_notes),
            len(digest.flagged_symbols),
        )
