"""Live workspace payloads for user-owned userbook_* paper fund personas."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

from src.config import Config, get_config
from src.dashboard.service import DashboardService, broker_label, persona_currency, region_label
from src.market.registry import get_market
from src.workspace.holding_intelligence import HoldingIntelligenceService
from src.workspace.user_book_personas import UserBookPersonaRepository, userbook_persona_config


def _minimal_userbook_persona_payload(persona_id: str, row: Dict[str, Any]) -> Dict[str, Any]:
    provision = row.get("provision_meta") or row.get("frozen_allocation") or {}
    template_id = str(row.get("template_persona_id") or provision.get("template_persona_id") or "balanced")
    book_name = str(provision.get("book_name") or persona_id)
    market_id = str(provision.get("market_id") or "india_equity")
    capital = float(provision.get("starting_capital") or 0)
    seed_symbols = list(provision.get("seed_symbols") or [])
    return {
        "id": persona_id,
        "metadata": {
            "fund_name": f"{book_name} · {template_id}",
            "description": f"Your paper persona · {template_id} strategy",
            "strategy_description": f"Full-universe {template_id} strategy from your watchlist",
            "market_id": market_id,
            "currency": "USD" if market_id == "us_equity" else "INR",
            "user_book": True,
            "source_book_id": provision.get("source_book_id"),
            "template_persona_id": template_id,
        },
        "overview": {
            "total_value": capital,
            "total_return_pct": 0.0,
            "position_count": len(seed_symbols),
            "cash_pct": 100.0 if not seed_symbols else 0.0,
            "has_data": True,
        },
        "warnings": ["Persona summary is limited until the next strategy run completes."],
        "source_freshness": {"analysis": "Pending"},
    }


def build_userbook_persona_summary(
    persona_id: str,
    config: Optional[Config] = None,
    *,
    row: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Lightweight card payload for persona list views."""
    base = config or get_config()
    if row is None:
        row = UserBookPersonaRepository(base).get_by_persona_id(persona_id)
    if row is None or row.get("status") != "active":
        raise ValueError(f"Active user book persona {persona_id!r} is unavailable.")

    summary = _minimal_userbook_persona_payload(persona_id, row)
    try:
        pcfg = userbook_persona_config(persona_id, base=base, row=row)
        service = DashboardService(config=pcfg)
        metrics = service.card_metrics()
        summary["overview"] = {
            "total_value": metrics.get("total_value"),
            "total_return_pct": metrics.get("total_return_pct"),
            "position_count": metrics.get("position_count"),
            "cash_pct": metrics.get("cash_pct"),
            "has_data": metrics.get("has_data"),
        }
        summary["source_freshness"] = {
            "prices": metrics.get("latest_nav_date"),
            "analysis": metrics.get("latest_analysis_date"),
        }
    except Exception:
        pass
    return summary


def build_userbook_persona_payload(
    persona_id: str,
    config: Optional[Config] = None,
    *,
    lite: bool = False,
) -> Dict[str, Any]:
    base = config or get_config()
    row = UserBookPersonaRepository(base).get_by_persona_id(persona_id)
    if row is None or row.get("status") != "active":
        raise ValueError(f"Active user book persona {persona_id!r} is unavailable.")

    try:
        return _build_userbook_persona_payload(persona_id, row, base, lite=lite)
    except Exception:
        return _minimal_userbook_persona_payload(persona_id, row)


def _build_userbook_persona_payload(
    persona_id: str,
    row: Dict[str, Any],
    base: Config,
    *,
    lite: bool = False,
) -> Dict[str, Any]:
    provision = row.get("provision_meta") or row.get("frozen_allocation") or {}
    pcfg = userbook_persona_config(persona_id, base=base, row=row)
    service = DashboardService(config=pcfg)
    intelligence = HoldingIntelligenceService(base)
    if lite:
        metrics = service.card_metrics()
        risk = service.risk_profile() or {}
        overview = {
            **metrics,
            "market_regime": risk.get("market_regime"),
            "warnings": [],
            "pending_count": 0,
            "latest_nav_date": metrics.get("latest_nav_date"),
            "latest_analysis_date": metrics.get("latest_analysis_date"),
        }
    else:
        overview = service.overview()
        risk = service.risk_profile() or {}
    performance = service.performance()
    news = service.digest()
    holdings_raw = service.holdings()
    holdings = holdings_raw if lite else intelligence.enrich_holdings(service, holdings_raw)
    market_id = str(provision.get("market_id") or pcfg.market_id)
    try:
        market_label = get_market(market_id).label
    except ValueError:
        market_label = market_id
    template_id = str(row.get("template_persona_id") or "balanced")
    book_name = str(provision.get("book_name") or persona_id)
    region = pcfg.market_region
    currency = persona_currency(pcfg, region)
    warnings = list(overview.get("warnings") or [])
    warnings.append("Your paper persona runs the selected strategy on the full market universe (DRY_RUN).")
    pending = row.get("pending_overrides") or {}
    if pending:
        warnings.append("Pending edits will apply on the next long-cycle run.")

    payload = {
        "id": persona_id,
        "metadata": {
            "persona_id": persona_id,
            "fund_name": f"{book_name} · {template_id}",
            "description": f"Your paper persona · {template_id} strategy",
            "strategy_description": f"Full-universe {template_id} strategy from your watchlist",
            "market_id": market_id,
            "market_label": market_label,
            "region": region,
            "region_label": region_label(region),
            "currency": currency,
            "broker": pcfg.broker_provider,
            "broker_label": broker_label(pcfg.broker_provider),
            "benchmark_label": str(pcfg.get("fund.benchmark_label") or ""),
            "capital": float(provision.get("starting_capital") or pcfg.secrets.initial_capital),
            "total_value": overview.get("total_value"),
            "position_count": overview.get("position_count"),
            "cash_pct": overview.get("cash_pct"),
            "pending_count": overview.get("pending_count"),
            "has_data": overview.get("has_data"),
            "intraday": False,
            "user_book": True,
            "source_book_id": provision.get("source_book_id"),
            "template_persona_id": template_id,
            "pending_overrides": pending,
            "applies_on": "next_long_cycle" if pending else None,
        },
        "overview": overview,
        "holdings": holdings,
        "performance": performance,
        "risk": risk,
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
    if not lite:
        payload["quarterly_cards"] = service.quarterly_report_cards()
    return payload


def list_user_persona_summaries(user_id: int, config: Optional[Config] = None) -> List[Dict[str, Any]]:
    base = config or get_config()
    rows = [
        row for row in UserBookPersonaRepository(base).list_active_for_user(user_id)
        if row.get("persona_id")
    ]
    if not rows:
        return []

    def _summary_for_row(row: Dict[str, Any]) -> Dict[str, Any]:
        persona_id = str(row["persona_id"])
        try:
            return build_userbook_persona_summary(persona_id, base, row=row)
        except ValueError:
            return _minimal_userbook_persona_payload(persona_id, row)

    if len(rows) == 1:
        return [_summary_for_row(rows[0])]

    workers = min(len(rows), 4)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(_summary_for_row, rows))


def list_user_persona_payloads(user_id: int, config: Optional[Config] = None) -> List[Dict[str, Any]]:
    return list_user_persona_summaries(user_id, config)
