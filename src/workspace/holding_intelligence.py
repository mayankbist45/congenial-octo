"""Assemble fundamentals, technicals, quarterly, earnings, and selection reasoning."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any, Dict, List, Optional

from src.config import Config, get_config, get_persona_config
from src.dashboard.service import DashboardService
from src.fundamentals.service import FundamentalsService
from src.journal.db import session
from src.journal.repository import JournalRepository
from src.market_data.prices import PriceService
from src.workspace.user_books import watchlist_universe_for_market


def _loads(value: Any, default: Any) -> Any:
    try:
        return json.loads(value) if value else default
    except (TypeError, ValueError):
        return default


def _fundamental_view(snapshot: Any) -> Dict[str, Any]:
    if snapshot is None:
        return {}
    data = asdict(snapshot)
    keep = (
        "symbol",
        "pe_ratio",
        "forward_pe",
        "roe_pct",
        "roce_pct",
        "debt_to_equity",
        "revenue_growth_pct",
        "earnings_growth_pct",
        "operating_margin_pct",
        "market_cap_bucket",
        "dividend_yield",
    )
    return {key: data.get(key) for key in keep if data.get(key) is not None}


def _technical_view(snapshot: Any) -> Dict[str, Any]:
    if snapshot is None:
        return {}
    data = asdict(snapshot)
    keep = (
        "symbol",
        "last_price",
        "sma_50",
        "sma_200",
        "rsi_14",
        "return_1m",
        "return_6m",
        "return_12m",
        "volatility_annualised",
        "above_sma_200",
        "golden_cross",
    )
    return {key: data.get(key) for key in keep if data.get(key) is not None}


class HoldingIntelligenceService:
    def __init__(self, config: Optional[Config] = None):
        self._config = config or get_config()

    def for_user_persona_holding(
        self,
        persona_id: str,
        symbol: str,
        *,
        user_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        from src.workspace.user_book_personas import UserBookPersonaNotFoundError, UserBookPersonaRepository

        row = UserBookPersonaRepository(self._config).get_by_persona_id(persona_id)
        if row is None:
            raise UserBookPersonaNotFoundError("User persona not found.")
        if user_id is not None and int(row.get("user_id") or 0) != int(user_id):
            raise UserBookPersonaNotFoundError("User persona not found.")

        pcfg = get_persona_config(persona_id, base=self._config)
        service = DashboardService(config=pcfg)
        holding = next((item for item in service.holdings() if item["symbol"] == symbol.upper()), None)
        if holding is None:
            raise KeyError(f"{symbol.upper()} is not in this persona portfolio.")

        bundle = self._strategy_bundle(service, symbol.upper(), holding)
        bundle["persona_id"] = persona_id
        bundle["selection"]["source"] = "strategy_engine"
        if row.get("pending_overrides"):
            bundle["pending_overrides"] = row["pending_overrides"]
            bundle["applies_on"] = "next_long_cycle"
        return bundle

    def for_watchlist_line(
        self,
        user_id: int,
        book_id: str,
        symbol: str,
        *,
        line: Optional[Dict[str, Any]] = None,
        market_id: str = "india_equity",
    ) -> Dict[str, Any]:
        from src.workspace.user_books import UserBookRepository

        repo = UserBookRepository(self._config)
        if line is None:
            line = repo.get_line(book_id, user_id, symbol.upper())
        book = repo.get_book(book_id, user_id)
        market_id = str(book.get("market_id") or market_id)
        universe = watchlist_universe_for_market(market_id)
        entry = universe.get(symbol.upper())
        prices = PriceService(self._config).get_last_price(symbol.upper())
        selection = {
            "source": "user_watchlist",
            "summary": f"Added to your watchlist on {line.get('entry_date') or line.get('added_at', '')[:10]}.",
            "notes": line.get("notes") or "",
            "entry_price": line.get("entry_price"),
            "entry_date": line.get("entry_date"),
            "added_at": line.get("added_at"),
            "target_amount": line.get("target_amount"),
            "weight_pct": line.get("weight_pct"),
        }
        research = self._market_research(symbol.upper(), market_id)
        return {
            "symbol": symbol.upper(),
            "company_name": entry.company_name if entry else symbol.upper(),
            "sector": entry.sector if entry else "Unknown",
            "market_id": market_id,
            "last_price": prices,
            "selection": selection,
            **research,
            "position": None,
            "evidence": [
                {
                    "type": "watchlist",
                    "label": "Your allocation",
                    "summary": selection["summary"],
                }
            ],
        }

    def intelligence_preview(
        self,
        service: DashboardService,
        symbol: str,
        thesis: str = "",
        *,
        holding: Optional[Dict[str, Any]] = None,
    ) -> str:
        if thesis:
            return thesis[:80] + ("…" if len(thesis) > 80 else "")
        parts: List[str] = []
        summary = service._quarterly_summary(symbol)
        if summary.get("quarterly_thesis_impact"):
            parts.append(f"Quarter {summary.get('latest_quarter') or '?'}: {summary['quarterly_thesis_impact']}")
        row = holding or next((item for item in service.holdings() if item["symbol"] == symbol.upper()), None)
        if row and row.get("composite_score") is not None:
            parts.append(f"Score {row['composite_score']:.2f}")
        return " · ".join(parts) if parts else "Research available"

    def enrich_holdings(self, service: DashboardService, holdings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        enriched: List[Dict[str, Any]] = []
        for holding in holdings:
            symbol = str(holding.get("symbol") or "").upper()
            item = dict(holding)
            item["intelligence_preview"] = self.intelligence_preview(
                service,
                symbol,
                str(holding.get("thesis") or ""),
                holding=item,
            )
            enriched.append(item)
        return enriched

    def _strategy_bundle(
        self,
        service: DashboardService,
        symbol: str,
        holding: Dict[str, Any],
    ) -> Dict[str, Any]:
        repo = JournalRepository(service._config)
        quarterly = service._quarterly_summary(symbol)
        history = repo.quarterly_history(symbol, limit=8)
        entry_proposal = self._latest_buy_proposal(repo, symbol)
        research = self._market_research(symbol, service._config.market_id)
        digest = service.digest() or {}
        holdings_notes = _loads(digest.get("holdings_notes"), {})
        flagged = set(_loads(digest.get("flagged_symbols"), []))

        selection = {
            "source": "strategy_engine",
            "summary": holding.get("thesis") or (entry_proposal or {}).get("rationale") or "",
            "thesis": holding.get("thesis") or (entry_proposal or {}).get("thesis") or "",
            "composite_score": holding.get("composite_score"),
            "entry_proposal": entry_proposal,
        }
        evidence: List[Dict[str, Any]] = []
        if selection["thesis"]:
            evidence.append(
                {"type": "thesis", "label": "Investment thesis", "summary": selection["thesis"]}
            )
        if entry_proposal:
            evidence.append(
                {
                    "type": "proposal",
                    "label": "Entry decision",
                    "summary": entry_proposal.get("rationale") or entry_proposal.get("thesis") or "",
                }
            )
        if quarterly.get("quarterly_summary"):
            evidence.append(
                {
                    "type": "quarterly",
                    "label": f"Quarter {quarterly.get('latest_quarter') or ''}".strip(),
                    "summary": quarterly["quarterly_summary"],
                }
            )

        return {
            "symbol": symbol,
            "company_name": holding.get("company_name") or symbol,
            "sector": holding.get("sector") or "Unknown",
            "selection": selection,
            "fundamentals": research.get("fundamentals") or {},
            "technicals": research.get("technicals") or {},
            "quarterly": {
                **quarterly,
                "revenue_series": service._align_series(history, "revenue") if history else {},
                "profit_series": service._align_series(history, "net_profit") if history else {},
                "opm_series": service._align_series(history, "opm_pct") if history else {},
            },
            "earnings": research.get("earnings") or {},
            "news": {
                "note": holdings_notes.get(symbol) or "",
                "flagged": symbol in flagged,
            },
            "position": holding,
            "evidence": evidence,
        }

    def _market_research(self, symbol: str, market_id: str) -> Dict[str, Any]:
        pcfg = self._config
        if market_id:
            from src.workspace.user_books import market_config

            pcfg = market_config(self._config, market_id)
        fundamentals = FundamentalsService(pcfg)
        snapshot = fundamentals.get(symbol)
        technicals = PriceService(pcfg).snapshots([symbol]).get(symbol)
        earnings = {}
        if snapshot and snapshot.earnings_surprise_pct is not None:
            earnings = {
                "surprise_pct": snapshot.earnings_surprise_pct,
                "quality_note": "Latest reported earnings vs consensus, when available.",
            }
        return {
            "fundamentals": _fundamental_view(snapshot),
            "technicals": _technical_view(technicals),
            "earnings": earnings,
        }

    def _latest_buy_proposal(self, repo: JournalRepository, symbol: str) -> Optional[Dict[str, Any]]:
        from src.journal.db import session

        with session(repo._config) as conn:
            row = conn.execute(
                """
                SELECT p.*, r.run_date
                  FROM proposals p
                  JOIN runs r ON r.id = p.run_id
                 WHERE p.persona_id = ? AND p.symbol = ? AND upper(p.action) IN ('BUY', 'ADD')
                 ORDER BY p.id DESC
                 LIMIT 1
                """,
                (repo._config.persona_id, symbol.upper()),
            ).fetchone()
        if not row:
            return None
        item = dict(row)
        item["trigger_types"] = _loads(item.get("trigger_types"), [])
        item["warnings"] = _loads(item.get("warnings"), [])
        return {
            "id": item["id"],
            "run_date": item.get("run_date"),
            "action": item.get("action"),
            "rationale": item.get("rationale") or "",
            "thesis": item.get("thesis") or "",
            "trigger_types": item.get("trigger_types") or [],
            "confidence": item.get("confidence"),
            "reference_price": item.get("reference_price"),
        }
