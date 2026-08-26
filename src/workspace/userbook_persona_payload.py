"""Live workspace payloads for user-owned userbook_* paper fund personas."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from src.config import Config, get_config
from src.dashboard.service import DashboardService, broker_label, persona_currency, region_label
from src.market.registry import get_market
from src.workspace.user_book_personas import UserBookPersonaRepository, userbook_persona_config


def build_userbook_persona_payload(persona_id: str, config: Optional[Config] = None) -> Dict[str, Any]:
    base = config or get_config()
    row = UserBookPersonaRepository(base).get_by_persona_id(persona_id)
    if row is None or row.get("status") != "active":
        raise ValueError(f"Active user book persona {persona_id!r} is unavailable.")

    frozen = row.get("frozen_allocation") or {}
    pcfg = userbook_persona_config(persona_id, base=base)
    service = DashboardService(config=pcfg)
    overview = service.overview()
    performance = service.performance()
    news = service.digest()
    market_id = str(frozen.get("market_id") or pcfg.market_id)
    try:
        market_label = get_market(market_id).label
    except ValueError:
        market_label = market_id
    template_id = str(row.get("template_persona_id") or "balanced")
    book_name = str(frozen.get("book_name") or persona_id)
    region = pcfg.market_region
    currency = persona_currency(pcfg, region)
    warnings = list(overview.get("warnings") or [])
    warnings.append("Your watchlist paper fund — trades only your frozen holdings (DRY_RUN).")

    return {
        "id": persona_id,
        "metadata": {
            "persona_id": persona_id,
            "fund_name": book_name,
            "description": f"Your watchlist paper fund · {template_id} strategy",
            "strategy_description": f"Frozen universe from watchlist · template {template_id}",
            "market_id": market_id,
            "market_label": market_label,
            "region": region,
            "region_label": region_label(region),
            "currency": currency,
            "broker": pcfg.broker_provider,
            "broker_label": broker_label(pcfg.broker_provider),
            "benchmark_label": str(pcfg.get("fund.benchmark_label") or ""),
            "capital": float(frozen.get("starting_capital") or pcfg.secrets.initial_capital),
            "total_value": overview.get("total_value"),
            "position_count": overview.get("position_count"),
            "cash_pct": overview.get("cash_pct"),
            "pending_count": overview.get("pending_count"),
            "has_data": overview.get("has_data"),
            "intraday": False,
            "user_book": True,
            "watchlist_book_id": frozen.get("book_id"),
            "template_persona_id": template_id,
        },
        "overview": overview,
        "holdings": service.holdings(),
        "sector_weights": service.sector_weights(),
        "performance": performance,
        "performance_series": service.nav_series(),
        "risk": service.risk_profile() or {},
        "runs": service.runs(),
        "news": news or {"has_data": False},
        "intraday": None,
        "warnings": warnings,
        "source_freshness": {
            "prices": overview.get("latest_nav_date"),
            "analysis": overview.get("latest_analysis_date") or (news or {}).get("run_date"),
            "news": (news or {}).get("run_date"),
        },
    }


def list_user_persona_payloads(user_id: int, config: Optional[Config] = None) -> List[Dict[str, Any]]:
    base = config or get_config()
    rows = UserBookPersonaRepository(base).list_active_for_user(user_id)
    payloads: List[Dict[str, Any]] = []
    for row in rows:
        persona_id = str(row.get("persona_id") or "")
        if not persona_id:
            continue
        try:
            payloads.append(build_userbook_persona_payload(persona_id, base))
        except ValueError:
            continue
    return payloads
