"""FastAPI dashboard.

The approval gate. Nothing the fund proposes reaches the broker until a human
approves it here, so the pages are built around one question: is there enough
information on screen to say yes or no responsibly? Each proposal shows the
trigger that produced it, the written rationale, the risk warnings attached
during validation, and the resulting position size.

The dashboard bind address is configured from settings rather than hardcoded.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from fastapi import BackgroundTasks, Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, HttpUrl
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from src.config import (
    Config,
    enabled_personas,
    get_config,
    get_persona_config,
    persona_description,
)
from src.dashboard import charts
from src.dashboard.market_ui import all_markets_summary, format_market_timestamp, market_label
from src.dashboard.service import (
    DashboardService,
    broker_label,
    format_money,
    money,
    number,
    pct,
    persona_currency,
    region_label,
    sign_class,
)
from src.logging_setup import get_logger
from src.operations import log_structured_event

logger = get_logger(__name__)

TEMPLATE_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"


class IpoResearchRequest(BaseModel):
    """A public-offer document to analyse in the IPO research workspace."""

    issuer: str
    source_url: HttpUrl
    document_text: Optional[str] = None


class WorkspaceProfileRequest(BaseModel):
    profile: Dict[str, Any]


class WorkspaceActionRequest(BaseModel):
    action: str
    note: Optional[str] = None


class AdminProposalRequest(BaseModel):
    reason: str = ""


def create_app(config: Optional[Config] = None, workspace_auth: Optional[Any] = None) -> FastAPI:
    config = config or get_config()
    default_service = DashboardService(config)
    if workspace_auth is None:
        from src.workspace.auth import WorkspaceAuth
        workspace_auth = WorkspaceAuth(config)

    app = FastAPI(title=default_service.fund_name, docs_url="/api/docs")
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    allowed_origins = config.get(
        "web.allowed_origins",
        ["http://localhost:5173", "http://127.0.0.1:5173"],
    )
    if not isinstance(allowed_origins, list):
        allowed_origins = [str(allowed_origins)]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[str(origin) for origin in allowed_origins],
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT"],
        allow_headers=["Authorization", "Content-Type"],
    )
    templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
    templates.env.globals["market_label"] = market_label
    templates.env.globals["format_market_timestamp"] = format_market_timestamp
    templates.env.filters["money"] = money
    templates.env.filters["money_currency"] = format_money
    templates.env.filters["pct"] = pct
    templates.env.filters["number"] = number
    templates.env.filters["sign"] = sign_class
    templates.env.globals["region_label"] = region_label
    templates.env.globals["broker_label"] = broker_label

    _persona_services: Dict[str, DashboardService] = {}

    def _log_dashboard_start() -> None:
        log_structured_event(
            logger,
            "dashboard_service",
            "start",
            bind_host=str(config.get("dashboard.host", "0.0.0.0")),
            bind_port=str(config.get("dashboard.port", 8000)),
        )

    app.add_event_handler("startup", _log_dashboard_start)

    @app.get("/healthz")
    def healthz() -> Dict[str, str]:
        """Public liveness check; workspace data remains authenticated."""
        return {"status": "ok"}

    def _get_service(persona: Optional[str] = None) -> DashboardService:
        if not persona:
            return default_service
        if persona not in _persona_services:
            pcfg = get_persona_config(persona, base=config)
            _persona_services[persona] = DashboardService(config=pcfg)
        return _persona_services[persona]

    _MARKET_NAV_ORDER = ("india_equity", "us_equity", "crypto_spot_us")

    def _persona_nav_groups() -> List[Dict[str, Any]]:
        """Persona picker groups: India → US → Crypto, Swing then Intraday."""
        from src.market.registry import get_market, list_markets

        buckets: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
        for pid in enabled_personas():
            try:
                pcfg = get_persona_config(pid, base=config)
            except ValueError:
                continue
            market_id = pcfg.market_id
            is_intraday = bool(pcfg.get("intraday.enabled", False))
            kind = "intraday" if is_intraday else "swing"
            try:
                market = get_market(market_id)
                market_label_text = market.label
                badge_class = market.display.badge_class
            except ValueError:
                market_label_text = market_id
                badge_class = "region-in"
            item = {
                "id": pid,
                "description": persona_description(pid),
                "short_name": pid,
                "intraday": is_intraday,
                "kind": kind,
                "kind_label": "Intraday" if is_intraday else "Swing",
                "market_id": market_id,
                "market_label": market_label_text,
                "badge_class": badge_class,
                "region": pcfg.market_region,
            }
            bucket = buckets.setdefault(market_id, {"swing": [], "intraday": []})
            bucket[kind].append(item)

        for bucket in buckets.values():
            bucket["swing"].sort(key=lambda r: r["id"])
            bucket["intraday"].sort(key=lambda r: r["id"])

        known = {m.id for m in list_markets()}
        ordered_ids = [mid for mid in _MARKET_NAV_ORDER if mid in buckets]
        ordered_ids.extend(
            sorted(mid for mid in buckets if mid not in _MARKET_NAV_ORDER and mid in known)
        )
        ordered_ids.extend(
            sorted(mid for mid in buckets if mid not in ordered_ids)
        )

        groups: List[Dict[str, Any]] = []
        for market_id in ordered_ids:
            bucket = buckets[market_id]
            try:
                market = get_market(market_id)
                label = market.label
                badge_class = market.display.badge_class
            except ValueError:
                label = market_id
                badge_class = "region-in"
            groups.append(
                {
                    "market_id": market_id,
                    "label": label,
                    "badge_class": badge_class,
                    "swing": bucket["swing"],
                    "intraday": bucket["intraday"],
                }
            )
        return groups

    def _persona_nav_items() -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        for group in _persona_nav_groups():
            items.extend(group["swing"])
            items.extend(group["intraday"])
        return items

    def _persona_context(
        persona: Optional[str],
    ) -> Tuple[DashboardService, Config, bool, Optional[Dict[str, Any]]]:
        svc = _get_service(persona)
        pcfg = get_persona_config(persona, base=config) if persona else config
        is_intraday = bool(pcfg.get("intraday.enabled", False))
        intraday_summary = svc.intraday_summary() if is_intraday else None
        return svc, pcfg, is_intraday, intraday_summary

    def render(
        request: Request,
        template: str,
        context: Dict[str, Any],
        service: Optional[DashboardService] = None,
        *,
        show_persona_selector: Optional[bool] = None,
        header_fund_name: Optional[str] = None,
    ) -> HTMLResponse:
        svc = service or default_service
        try:
            pending_count = len(svc.pending_proposals())
        except Exception:
            pending_count = 0
        persona_id = svc.persona_id
        region = svc._config.market_region
        currency = persona_currency(svc._config, region)
        is_intraday = bool(svc._config.get("intraday.enabled", False))
        persona_qs = (
            f"?persona={persona_id}"
            if persona_id and persona_id != "balanced"
            else ""
        )
        page = context.get("page", "")
        if show_persona_selector is None:
            show_persona_selector = page not in ("personas", "intraday")
        swing_only_picker = bool(context.pop("swing_only_picker", False))
        persona_groups = _persona_nav_groups()
        if swing_only_picker:
            persona_groups = [
                {
                    **group,
                    "swing": list(group.get("swing") or []),
                    "intraday": [],
                }
                for group in persona_groups
                if group.get("swing")
            ]
        personas = []
        for group in persona_groups:
            personas.extend(group.get("swing") or [])
            personas.extend(group.get("intraday") or [])
        base = {
            "fund_name": header_fund_name or svc.fund_name,
            "benchmark_label": svc.benchmark_label,
            "is_dry_run": svc.is_dry_run,
            "pending_count": pending_count,
            "current_persona": persona_id,
            "persona_description": persona_description(persona_id),
            "personas": personas,
            "persona_groups": persona_groups,
            "currency": currency,
            "region": region,
            "region_label": region_label(region),
            "is_intraday": is_intraday,
            "persona_qs": persona_qs,
            "show_persona_selector": show_persona_selector,
        }
        base.update(context)
        return templates.TemplateResponse(request, template, base)

    # ------------------------------------------------------------------
    # Pages
    # ------------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    def overview(
        request: Request, persona: Optional[str] = Query(default=None)
    ) -> HTMLResponse:
        svc, pcfg, is_intraday, intraday_summary = _persona_context(persona)

        if is_intraday and intraday_summary:
            overview_data = svc.intraday_dashboard_overview(intraday_summary)
            performance_data = {
                "total_return_pct": overview_data["total_return_pct"],
                "excess_return_pct": None,
                "benchmark_return_pct": None,
            }
            chart_html = ""
        elif is_intraday:
            region = pcfg.market_region
            capital = float(pcfg.secrets.initial_capital)
            overview_data = {
                "has_data": False,
                "is_intraday": True,
                "region": region,
                "region_label": region_label(region),
                "broker_label": broker_label(pcfg.broker_provider),
                "currency": persona_currency(pcfg, region),
                "starting_capital": capital,
                "total_net_pnl": 0.0,
                "total_return_pct": 0.0,
                "total_trades": 0,
                "win_rate_pct": 0.0,
                "expectancy_r": 0.0,
                "start_date": None,
                "end_date": None,
                "active": False,
                "last_scan_at": None,
                "lockout_reason": None,
            }
            performance_data = {
                "total_return_pct": 0.0,
                "excess_return_pct": None,
                "benchmark_return_pct": None,
            }
            chart_html = ""
        else:
            overview_data = svc.overview()
            performance_data = {
                "total_return_pct": overview_data.get("total_return_pct"),
                "excess_return_pct": overview_data.get("excess_return_pct"),
                "benchmark_return_pct": svc.performance().get("benchmark_return_pct"),
            }
            series = svc.nav_series()
            chart_html = charts.line_chart(
                series["dates"],
                series["fund"],
                series["benchmark"],
                svc.benchmark_label,
            )

        return render(
            request,
            "overview.html",
            {
                "page": "overview",
                "overview": overview_data,
                "proposals": svc.pending_proposals() if not is_intraday else [],
                "performance": performance_data,
                "chart": chart_html,
                "digest": svc.digest() if not is_intraday else None,
                "region_labels": {"IN": region_label("IN"), "US": region_label("US")},
            },
            service=svc,
        )

    @app.get("/proposals", response_class=HTMLResponse)
    def proposals(
        request: Request, persona: Optional[str] = Query(default=None)
    ) -> HTMLResponse:
        svc, pcfg, is_intraday, intraday_summary = _persona_context(persona)
        # Proposals is swing-only — bounce intraday picks to the first swing book.
        if is_intraday:
            swing_groups = _persona_nav_groups()
            swing_ids = [
                item["id"]
                for group in swing_groups
                for item in (group.get("swing") or [])
            ]
            fallback = "balanced" if "balanced" in swing_ids else (swing_ids[0] if swing_ids else None)
            if fallback:
                return RedirectResponse(url=f"/proposals?persona={fallback}", status_code=302)
        return render(
            request,
            "proposals.html",
            {
                "page": "proposals",
                "proposals": svc.pending_proposals(),
                "approved": svc.approved_proposals(),
                "history": svc.recent_decisions(),
                "mandate": svc.mandate_summary(),
                "analytics": svc.analytics_summary(),
                "intraday_summary": None,
                "swing_only_picker": True,
            },
            service=svc,
        )

    @app.get("/holdings", response_class=HTMLResponse)
    def holdings(
        request: Request, persona: Optional[str] = Query(default=None)
    ) -> HTMLResponse:
        svc, pcfg, is_intraday, intraday_summary = _persona_context(persona)
        sectors = [] if is_intraday else svc.sector_weights()
        holdings_rows = svc.holdings() if not is_intraday else []
        open_positions = (
            (intraday_summary or {}).get("open_positions") or []
            if is_intraday
            else []
        )
        completed_trades = (
            (intraday_summary or {}).get("trades")
            or (intraday_summary or {}).get("completed_trades")
            or []
            if is_intraday
            else []
        )
        return render(
            request,
            "holdings.html",
            {
                "page": "holdings",
                "holdings": holdings_rows,
                "open_positions": open_positions,
                "completed_trades": completed_trades,
                "intraday_summary": intraday_summary,
                "sectors": sectors,
                "sector_chart": charts.bar_chart(
                    [s["sector"] for s in sectors],
                    [s["weight_pct"] or 0.0 for s in sectors],
                )
                if sectors
                else "",
                "max_sector_pct": pcfg.get("risk.max_sector_pct"),
                "max_position_pct": pcfg.get("risk.max_position_pct"),
                "mandate": svc.mandate_summary(),
                "analytics": svc.analytics_summary(),
            },
            service=svc,
        )

    @app.get("/news", response_class=HTMLResponse)
    def news(
        request: Request, persona: Optional[str] = Query(default=None)
    ) -> HTMLResponse:
        svc, pcfg, is_intraday, intraday_summary = _persona_context(persona)
        return render(
            request,
            "news.html",
            {
                "page": "news",
                "digest": None if is_intraday else svc.digest(),
                "intraday_news": svc.intraday_news_context(intraday_summary)
                if is_intraday
                else None,
            },
            service=svc,
        )

    @app.get("/performance", response_class=HTMLResponse)
    def performance(
        request: Request, persona: Optional[str] = Query(default=None)
    ) -> HTMLResponse:
        svc, pcfg, is_intraday, intraday_summary = _persona_context(persona)

        if is_intraday and intraday_summary:
            perf = svc.intraday_performance(intraday_summary)
            chart_html = ""
            attribution: List[Dict[str, Any]] = []
            attribution_chart = ""
            risk = None
            completed_trades = (
                intraday_summary.get("trades")
                or intraday_summary.get("completed_trades")
                or []
            )
        elif is_intraday:
            perf = {"is_intraday": True, "start_date": None, "end_date": None}
            chart_html = ""
            attribution = []
            attribution_chart = ""
            risk = None
            completed_trades = []
        else:
            completed_trades = []
            perf = svc.performance()
            series = svc.nav_series()
            chart_html = charts.line_chart(
                series["dates"],
                series["fund"],
                series["benchmark"],
                svc.benchmark_label,
            )
            attribution = svc.attribution()
            attribution_chart = charts.bar_chart(
                [row["symbol"] for row in attribution[:15]],
                [row["contribution_pct"] or 0.0 for row in attribution[:15]],
            )
            risk = svc.risk_profile()

        return render(
            request,
            "performance.html",
            {
                "page": "performance",
                "performance": perf,
                "chart": chart_html,
                "attribution": attribution,
                "attribution_chart": attribution_chart,
                "risk": risk,
                "intraday_summary": intraday_summary,
                "completed_trades": completed_trades,
            },
            service=svc,
        )

    @app.get("/journal", response_class=HTMLResponse)
    def journal(
        request: Request, persona: Optional[str] = Query(default=None)
    ) -> HTMLResponse:
        svc, pcfg, is_intraday, intraday_summary = _persona_context(persona)
        if is_intraday:
            completed_trades = (
                (intraday_summary or {}).get("trades")
                or (intraday_summary or {}).get("completed_trades")
                or []
            )
            skipped = (intraday_summary or {}).get("skipped_signals") or []
            return render(
                request,
                "journal.html",
                {
                    "page": "journal",
                    "trades": [],
                    "runs": [],
                    "intraday_summary": intraday_summary,
                    "completed_trades": completed_trades,
                    "skipped_signals": skipped,
                },
                service=svc,
            )
        return render(
            request,
            "journal.html",
            {
                "page": "journal",
                "trades": svc.trades(),
                "runs": svc.runs(),
                "intraday_summary": None,
                "completed_trades": [],
                "skipped_signals": [],
            },
            service=svc,
        )

    @app.get("/journal/{run_id}", response_class=HTMLResponse)
    def run_detail(
        request: Request,
        run_id: int,
        persona: Optional[str] = Query(default=None),
    ) -> HTMLResponse:
        svc = _get_service(persona)
        run = svc.run_detail(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Run not found")
        return render(
            request,
            "run.html",
            {
                "page": "journal",
                "run": run,
                "briefing": svc.briefing(run["run_date"]),
            },
            service=svc,
        )

    @app.get("/operations", response_class=HTMLResponse)
    def operations_page(request: Request) -> HTMLResponse:
        return render(
            request,
            "operations.html",
            {
                "page": "operations",
                "operations": default_service.operations_overview(),
            },
            service=default_service,
            show_persona_selector=False,
        )

    @app.get("/markets", response_class=HTMLResponse)
    def markets_page(request: Request) -> HTMLResponse:
        grouped = default_service.personas_by_market()
        return render(
            request,
            "personas.html",
            {
                "page": "personas",
                "markets": all_markets_summary(),
                "markets_grouped": grouped,
                "regions": default_service.personas_by_region(),
                "region_labels": {
                    m["legacy_region"]: m["label"] for m in all_markets_summary()
                },
            },
            header_fund_name="Trade Bot Dashboard",
        )

    @app.get("/personas", response_class=HTMLResponse)
    def personas_page(request: Request) -> HTMLResponse:
        grouped = default_service.personas_by_market()
        return render(
            request,
            "personas.html",
            {
                "page": "personas",
                "markets": all_markets_summary(),
                "markets_grouped": grouped,
                "regions": default_service.personas_by_region(),
                "region_labels": {
                    m["legacy_region"]: m["label"] for m in all_markets_summary()
                },
            },
            header_fund_name="Trade Bot Dashboard",
        )

    @app.get("/intraday", response_class=HTMLResponse)
    def intraday_page(
        request: Request,
        region: Optional[str] = None,
        market: Optional[str] = None,
    ) -> HTMLResponse:
        markets = all_markets_summary()
        active_region = region.upper() if region else None
        active_market = market.strip() if market else None
        if active_region and active_region not in {m["legacy_region"] for m in markets}:
            raise HTTPException(status_code=400, detail="unknown region")
        if active_market and active_market not in {m["id"] for m in markets}:
            raise HTTPException(status_code=400, detail="unknown market")
        if active_market:
            personas = default_service.intraday_personas(market_id=active_market)
        elif active_region:
            personas = default_service.intraday_personas(active_region)
        else:
            personas = default_service.intraday_personas()
        by_market = default_service.personas_by_market()
        intraday_by_market = {
            mid: [p for p in rows if p.get("intraday")]
            for mid, rows in by_market.items()
        }
        return render(
            request,
            "intraday.html",
            {
                "page": "intraday",
                "personas": personas,
                "markets": markets,
                "intraday_by_market": intraday_by_market,
                "active_region": active_region,
                "active_market": active_market,
                "region_labels": {m["legacy_region"]: m["label"] for m in markets},
            },
            header_fund_name="Trade Bot Dashboard",
        )

    @app.get("/intraday/{persona_id}", response_class=HTMLResponse)
    def intraday_detail(request: Request, persona_id: str) -> HTMLResponse:
        try:
            pcfg = get_persona_config(persona_id, base=config)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if not pcfg.get("intraday.enabled", False):
            raise HTTPException(status_code=404, detail="Not an intraday persona")

        persona_svc = DashboardService(config=pcfg)
        summary = persona_svc.intraday_summary()
        region = pcfg.market_region
        market = pcfg.market
        currency = persona_currency(pcfg, region)
        capital = pcfg.secrets.initial_capital
        persona = {
            "persona_id": persona_id,
            "description": persona_description(persona_id),
            "market_id": market.id,
            "region": region,
            "region_label": region_label(region),
            "currency": currency,
            "capital": capital,
            "broker_label": market.display.broker_label or broker_label(pcfg.broker_provider),
        }
        return render(
            request,
            "intraday_detail.html",
            {
                "page": "intraday",
                "persona": persona,
                "summary": summary,
            },
            service=persona_svc,
            header_fund_name="Trade Bot Dashboard",
        )

    # ------------------------------------------------------------------
    # Approval actions
    # ------------------------------------------------------------------
    @app.post("/proposals/{proposal_id}/approve")
    def approve(
        proposal_id: int, persona: Optional[str] = Query(default=None)
    ) -> RedirectResponse:
        svc = _get_service(persona)
        if not svc.approve(proposal_id):
            raise HTTPException(
                status_code=409, detail="Proposal is not pending a decision"
            )
        redirect = f"/proposals?persona={persona}" if persona else "/proposals"
        return RedirectResponse(redirect, status_code=303)

    @app.post("/proposals/{proposal_id}/reject")
    def reject(
        proposal_id: int,
        reason: str = Form(default=""),
        persona: Optional[str] = Query(default=None),
    ) -> RedirectResponse:
        svc = _get_service(persona)
        if not svc.reject(proposal_id, reason):
            raise HTTPException(
                status_code=409, detail="Proposal is not pending a decision"
            )
        redirect = f"/proposals?persona={persona}" if persona else "/proposals"
        return RedirectResponse(redirect, status_code=303)

    @app.post("/proposals/approve-all")
    def approve_all(
        persona: Optional[str] = Query(default=None),
    ) -> RedirectResponse:
        svc = _get_service(persona)
        count = svc.approve_all()
        logger.info("Approved %d proposal(s) in bulk", count)
        redirect = f"/proposals?persona={persona}" if persona else "/proposals"
        return RedirectResponse(redirect, status_code=303)

    @app.post("/proposals/reject-all")
    def reject_all(
        persona: Optional[str] = Query(default=None),
    ) -> RedirectResponse:
        svc = _get_service(persona)
        count = svc.reject_all()
        logger.info("Rejected %d proposal(s) in bulk", count)
        redirect = f"/proposals?persona={persona}" if persona else "/proposals"
        return RedirectResponse(redirect, status_code=303)

    # ------------------------------------------------------------------
    # JSON API, for scripts and the separately deployed web app.
    # ------------------------------------------------------------------
    @app.get("/api/overview")
    def api_overview(persona: Optional[str] = Query(default=None)) -> JSONResponse:
        svc = _get_service(persona)
        return JSONResponse(svc.overview())

    @app.get("/api/holdings")
    def api_holdings(persona: Optional[str] = Query(default=None)) -> JSONResponse:
        svc = _get_service(persona)
        return JSONResponse(svc.holdings())

    @app.get("/api/proposals")
    def api_proposals(persona: Optional[str] = Query(default=None)) -> JSONResponse:
        svc = _get_service(persona)
        return JSONResponse(
            {
                "pending": svc.pending_proposals(),
                "approved": svc.approved_proposals(),
            }
        )

    @app.get("/api/performance")
    def api_performance(persona: Optional[str] = Query(default=None)) -> JSONResponse:
        svc = _get_service(persona)
        return JSONResponse(
            {"report": svc.performance(), "series": svc.nav_series()}
        )

    @app.get("/api/risk")
    def api_risk(persona: Optional[str] = Query(default=None)) -> JSONResponse:
        svc = _get_service(persona)
        return JSONResponse(svc.risk_profile() or {})

    @app.get("/api/personas")
    def api_personas() -> JSONResponse:
        return JSONResponse(default_service.personas_by_market())

    @app.get("/api/markets")
    def api_markets() -> JSONResponse:
        return JSONResponse(all_markets_summary())

    @app.get("/api/intraday")
    def api_intraday(
        region: Optional[str] = None,
        market: Optional[str] = None,
    ) -> JSONResponse:
        if market:
            return JSONResponse(default_service.intraday_personas(market_id=market))
        active = region.upper() if region else None
        return JSONResponse(default_service.intraday_personas(active))

    @app.get("/api/v1/ipos")
    def api_ipo_issues(limit: int = Query(default=24, ge=1, le=100)) -> JSONResponse:
        from src.ipo.service import IpoResearchService

        try:
            return JSONResponse({"issues": IpoResearchService(config).list_issues(limit)})
        except Exception as exc:
            logger.warning("Unable to load SEBI public issues: %s", exc)
            raise HTTPException(status_code=502, detail="Unable to load SEBI public issues.") from exc

    @app.post("/api/v1/ipos/research")
    def api_ipo_research(payload: IpoResearchRequest) -> JSONResponse:
        from src.ipo.service import IpoResearchError, IpoResearchService

        source_host = payload.source_url.host or ""
        if source_host != "sebi.gov.in" and not source_host.endswith(".sebi.gov.in"):
            raise HTTPException(
                status_code=422,
                detail="IPO research accepts official SEBI document URLs only.",
            )
        try:
            dossier = IpoResearchService(config).research(
                issuer=payload.issuer,
                source_url=str(payload.source_url),
                document_text=payload.document_text,
            )
        except (IpoResearchError, requests.RequestException) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return JSONResponse(dossier)

    # ------------------------------------------------------------------
    # Public read-only research workspace. These routes intentionally expose no
    # execution capability and only return delayed/end-of-day research data.
    # ------------------------------------------------------------------
    def _workspace_service():
        from src.workspace.service import WorkspaceService
        return WorkspaceService(config)

    def _workspace_snapshot() -> Dict[str, Any]:
        from src.workspace.dashboard_snapshot import DashboardSnapshotService

        snapshot = DashboardSnapshotService(config).latest()
        if snapshot is None:
            raise HTTPException(
                status_code=404,
                detail="No delayed/EOD dashboard snapshot has been published yet.",
            )
        return snapshot

    def _workspace_persona(persona_id: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        snapshot = _workspace_snapshot()
        persona = next(
            (row for row in snapshot.get("personas", []) if row.get("id") == persona_id),
            None,
        )
        if persona is None:
            raise HTTPException(status_code=404, detail="Persona is unavailable in the latest delayed/EOD snapshot.")
        return snapshot, persona

    def _workspace_response(snapshot: Dict[str, Any], **payload: Any) -> JSONResponse:
        return JSONResponse({
            **payload,
            "as_of": snapshot["as_of"],
            "snapshot_date": snapshot["snapshot_date"],
            "data_status": snapshot.get("data_status", "delayed_eod"),
            "disclaimer": snapshot.get("disclaimer", "Research workspace. Not investment advice. No live order execution."),
        })

    def _admin_identity(identity: Dict[str, Any] = Depends(workspace_auth.identity)) -> Dict[str, Any]:
        if identity.get("role") != "admin":
            raise HTTPException(status_code=403, detail="Administrator access is required.")
        return identity

    @app.get("/api/v1/workspace/me")
    def workspace_me(identity: Dict[str, Any] = Depends(workspace_auth.identity)) -> JSONResponse:
        return JSONResponse({
            "user": {
                "id": identity["id"],
                "email": identity["email"],
                "role": identity["role"],
                "display_name": identity.get("display_name"),
                "avatar_url": identity.get("avatar_url"),
                "created_at": identity.get("created_at"),
                "last_seen_at": identity.get("last_seen_at"),
            },
            "disclaimer": "Research workspace. Not investment advice. No live order execution.",
        })

    @app.get("/api/v1/workspace/profile")
    def workspace_profile(identity: Dict[str, Any] = Depends(workspace_auth.identity)) -> JSONResponse:
        service = _workspace_service()
        return JSONResponse({"profile": service.repo.profile(int(identity["id"])), "disclaimer": "Research workspace. Not investment advice. No live order execution."})

    @app.get("/api/v1/workspace/dashboard")
    def workspace_dashboard(identity: Dict[str, Any] = Depends(workspace_auth.identity)) -> JSONResponse:
        return JSONResponse(_workspace_snapshot())

    @app.get("/api/v1/workspace/personas")
    def workspace_personas(identity: Dict[str, Any] = Depends(workspace_auth.identity)) -> JSONResponse:
        snapshot = _workspace_snapshot()
        summaries = []
        for persona in snapshot.get("personas", []):
            summaries.append({
                "id": persona["id"],
                "metadata": persona.get("metadata", {}),
                "overview": persona.get("overview", {}),
                "source_freshness": persona.get("source_freshness", {}),
                "warnings": persona.get("warnings", []),
            })
        return _workspace_response(snapshot, personas=summaries)

    @app.get("/api/v1/workspace/personas/{persona_id}")
    def workspace_persona(persona_id: str, identity: Dict[str, Any] = Depends(workspace_auth.identity)) -> JSONResponse:
        snapshot, persona = _workspace_persona(persona_id)
        # Holdings and portfolio context stay frozen at the EOD publication
        # boundary, while journalled run history is safe to expose immediately.
        view = dict(persona)
        view["runs"] = _get_service(persona_id).runs()
        return _workspace_response(snapshot, persona=view)

    @app.get("/api/v1/workspace/personas/{persona_id}/holdings")
    def workspace_persona_holdings(persona_id: str, identity: Dict[str, Any] = Depends(workspace_auth.identity)) -> JSONResponse:
        snapshot, persona = _workspace_persona(persona_id)
        return _workspace_response(snapshot, persona_id=persona_id, holdings=persona.get("holdings", []), sector_weights=persona.get("sector_weights", []), warnings=persona.get("warnings", []), source_freshness=persona.get("source_freshness", {}))

    @app.get("/api/v1/workspace/personas/{persona_id}/performance")
    def workspace_persona_performance(persona_id: str, identity: Dict[str, Any] = Depends(workspace_auth.identity)) -> JSONResponse:
        snapshot, persona = _workspace_persona(persona_id)
        return _workspace_response(snapshot, persona_id=persona_id, performance=persona.get("performance", {}), series=persona.get("performance_series", {}), source_freshness=persona.get("source_freshness", {}))

    @app.get("/api/v1/workspace/personas/{persona_id}/risk")
    def workspace_persona_risk(persona_id: str, identity: Dict[str, Any] = Depends(workspace_auth.identity)) -> JSONResponse:
        snapshot, persona = _workspace_persona(persona_id)
        return _workspace_response(snapshot, persona_id=persona_id, risk=persona.get("risk", {}), warnings=persona.get("warnings", []), source_freshness=persona.get("source_freshness", {}))

    @app.get("/api/v1/workspace/personas/{persona_id}/news")
    def workspace_persona_news(persona_id: str, identity: Dict[str, Any] = Depends(workspace_auth.identity)) -> JSONResponse:
        snapshot, persona = _workspace_persona(persona_id)
        return _workspace_response(snapshot, persona_id=persona_id, news=persona.get("news", {"has_data": False}), intraday=persona.get("intraday"), source_freshness=persona.get("source_freshness", {}))

    @app.get("/api/v1/workspace/personas/{persona_id}/runs")
    def workspace_persona_runs(persona_id: str, identity: Dict[str, Any] = Depends(workspace_auth.identity)) -> JSONResponse:
        snapshot, persona = _workspace_persona(persona_id)
        return _workspace_response(snapshot, persona_id=persona_id, runs=_get_service(persona_id).runs(), source_freshness=persona.get("source_freshness", {}))

    @app.get("/api/v1/workspace/personas/{persona_id}/runs/{run_id}")
    def workspace_persona_run_detail(
        persona_id: str,
        run_id: int,
        identity: Dict[str, Any] = Depends(workspace_auth.identity),
    ) -> JSONResponse:
        """Return one auditable run, scoped to the persona in the URL."""
        snapshot, persona = _workspace_persona(persona_id)
        run = _get_service(persona_id).run_detail(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Run is unavailable for this persona.")
        return _workspace_response(
            snapshot,
            persona_id=persona_id,
            persona={
                "id": persona_id,
                "metadata": persona.get("metadata", {}),
                "source_freshness": persona.get("source_freshness", {}),
            },
            run=run,
            recent_runs=_get_service(persona_id).runs(),
            source_freshness=persona.get("source_freshness", {}),
        )

    @app.get("/api/v1/workspace/news")
    def workspace_news(identity: Dict[str, Any] = Depends(workspace_auth.identity)) -> JSONResponse:
        snapshot = _workspace_snapshot()
        entries = []
        for persona in snapshot.get("personas", []):
            news = persona.get("news") or {}
            if not news.get("has_data", bool(news.get("run_date"))):
                continue
            entries.append({
                "persona_id": persona["id"],
                "persona": persona.get("metadata", {}),
                "news": news,
                "source_freshness": persona.get("source_freshness", {}),
            })
        entries.sort(key=lambda item: (item["news"].get("high_impact_count", 0), item["news"].get("run_date") or ""), reverse=True)
        return _workspace_response(snapshot, news=entries)

    @app.get("/api/v1/workspace/operations")
    def workspace_operations(identity: Dict[str, Any] = Depends(_admin_identity)) -> JSONResponse:
        """Read-only operations visibility for administrators only."""
        snapshot = _workspace_snapshot()
        return _workspace_response(snapshot, viewer_role=identity.get("role", "member"), operations={
            "overview": default_service.operations_overview(),
            "proposals": {
                "pending": default_service.pending_proposals(),
                "approved": default_service.approved_proposals(),
                "recent": default_service.recent_decisions(),
            },
            "audit_events": default_service.recent_decisions(),
        })

    @app.get("/api/v1/workspace/dashboard/stocks/{symbol}")
    def workspace_dashboard_stock(symbol: str, identity: Dict[str, Any] = Depends(workspace_auth.identity)) -> JSONResponse:
        snapshot = _workspace_snapshot()
        holding = next((item for item in (snapshot or {}).get("holdings", []) if item.get("symbol") == symbol.upper()), None)
        if holding is None:
            raise HTTPException(status_code=404, detail="Stock is not in the latest delayed/EOD dashboard snapshot.")
        return JSONResponse({
            "stock": holding,
            "as_of": snapshot["as_of"],
            "data_status": snapshot["data_status"],
            "evidence": [{"type": "fund_journal", "label": "Fund holding thesis", "summary": holding.get("thesis") or "No thesis was persisted for this holding."}],
            "disclaimer": snapshot["disclaimer"],
        })

    @app.get("/api/v1/workspace/personas/{persona_id}/stocks/{symbol}")
    def workspace_persona_stock(persona_id: str, symbol: str, identity: Dict[str, Any] = Depends(workspace_auth.identity)) -> JSONResponse:
        snapshot, persona = _workspace_persona(persona_id)
        holding = next((item for item in persona.get("holdings", []) if item.get("symbol") == symbol.upper()), None)
        if holding is None:
            raise HTTPException(status_code=404, detail="Stock is not in this persona's latest delayed/EOD snapshot.")
        return _workspace_response(snapshot, persona_id=persona_id, stock=holding, evidence=[{
            "type": "fund_journal",
            "label": "Fund holding thesis",
            "summary": holding.get("thesis") or "No thesis was persisted for this holding.",
        }], source_freshness=persona.get("source_freshness", {}), warnings=persona.get("warnings", []))

    @app.put("/api/v1/workspace/profile")
    def update_workspace_profile(payload: WorkspaceProfileRequest, identity: Dict[str, Any] = Depends(_admin_identity)) -> JSONResponse:
        service = _workspace_service()
        profile = service.repo.save_profile(int(identity["id"]), payload.profile)
        return JSONResponse({"profile": profile, "disclaimer": "Research workspace. Not investment advice. No live order execution."})

    @app.post("/api/v1/workspace/portfolios", status_code=202)
    def create_workspace_portfolio(background_tasks: BackgroundTasks, identity: Dict[str, Any] = Depends(_admin_identity)) -> JSONResponse:
        service = _workspace_service()
        job = service.repo.create_job(int(identity["id"]))
        background_tasks.add_task(service.generate, job["id"], int(identity["id"]))
        return JSONResponse({"job": job, "disclaimer": "Research workspace. Not investment advice. No live order execution."}, status_code=202)

    @app.get("/api/v1/workspace/jobs/{job_id}")
    def workspace_job(job_id: str, identity: Dict[str, Any] = Depends(workspace_auth.identity)) -> JSONResponse:
        try:
            job = _workspace_service().repo.job(job_id, int(identity["id"]))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return JSONResponse({"job": job, "disclaimer": "Research workspace. Not investment advice. No live order execution."})

    @app.get("/api/v1/workspace/portfolios/latest")
    def latest_workspace_portfolio(identity: Dict[str, Any] = Depends(workspace_auth.identity)) -> JSONResponse:
        portfolio = _workspace_service().repo.latest_portfolio(int(identity["id"]))
        if portfolio is None:
            raise HTTPException(status_code=404, detail="No model portfolio has been generated yet.")
        return JSONResponse(portfolio)

    @app.get("/api/v1/workspace/portfolios/{portfolio_id}")
    def workspace_portfolio(portfolio_id: str, identity: Dict[str, Any] = Depends(workspace_auth.identity)) -> JSONResponse:
        try:
            portfolio = _workspace_service().repo.portfolio(portfolio_id, int(identity["id"]))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return JSONResponse(portfolio)

    @app.get("/api/v1/workspace/stocks/{symbol}")
    def workspace_stock(symbol: str, identity: Dict[str, Any] = Depends(workspace_auth.identity)) -> JSONResponse:
        portfolio = _workspace_service().repo.latest_portfolio(int(identity["id"]))
        holding = next((item for item in (portfolio or {}).get("holdings", []) if item["symbol"] == symbol.upper()), None)
        if holding is None:
            raise HTTPException(status_code=404, detail="Stock research is unavailable in the latest model portfolio.")
        return JSONResponse({"stock": holding, "as_of": portfolio["as_of"], "data_status": "delayed_eod", "evidence": [{"type": "model", "label": "Research screen", "summary": holding["reason"]}], "disclaimer": "Research workspace. Not investment advice. No live order execution."})

    @app.get("/api/v1/workspace/inbox")
    def workspace_inbox(identity: Dict[str, Any] = Depends(workspace_auth.identity)) -> JSONResponse:
        return JSONResponse({"events": _workspace_service().repo.inbox(int(identity["id"])), "disclaimer": "Research workspace. Not investment advice. No live order execution."})

    @app.post("/api/v1/workspace/events/{event_id}/acknowledge")
    def acknowledge_workspace_event(event_id: str, identity: Dict[str, Any] = Depends(_admin_identity)) -> JSONResponse:
        try:
            _workspace_service().repo.acknowledge_event(event_id, int(identity["id"]))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return JSONResponse({"status": "acknowledged"})

    @app.post("/api/v1/workspace/portfolio-actions")
    def record_workspace_action(payload: WorkspaceActionRequest, identity: Dict[str, Any] = Depends(_admin_identity)) -> JSONResponse:
        service = _workspace_service()
        event_id = service.repo.add_event(int(identity["id"]), {"type": "user_review", "impact": "low", "title": payload.action, "detail": payload.note or "Review recorded without broker execution.", "created_at": datetime.now(timezone.utc).isoformat()})
        return JSONResponse({"event_id": event_id, "status": "recorded", "disclaimer": "Research workspace. Not investment advice. No live order execution."})

    # ------------------------------------------------------------------
    # React admin console contracts. These reuse the existing read-side
    # service and preserve its human approval gate without serving Jinja.
    # ------------------------------------------------------------------
    @app.get("/api/v1/admin/overview")
    def admin_overview(_: Dict[str, Any] = Depends(_admin_identity)) -> JSONResponse:
        return JSONResponse(default_service.overview())

    @app.get("/api/v1/admin/proposals")
    def admin_proposals(_: Dict[str, Any] = Depends(_admin_identity)) -> JSONResponse:
        return JSONResponse({"pending": default_service.pending_proposals(), "approved": default_service.approved_proposals(), "recent": default_service.recent_decisions()})

    @app.post("/api/v1/admin/proposals/{proposal_id}/approve")
    def admin_approve_proposal(proposal_id: int, _: Dict[str, Any] = Depends(_admin_identity)) -> JSONResponse:
        if not default_service.approve(proposal_id):
            raise HTTPException(status_code=409, detail="Proposal cannot be approved in its current state.")
        return JSONResponse({"status": "approved", "proposal_id": proposal_id})

    @app.post("/api/v1/admin/proposals/{proposal_id}/reject")
    def admin_reject_proposal(proposal_id: int, payload: AdminProposalRequest, _: Dict[str, Any] = Depends(_admin_identity)) -> JSONResponse:
        if not default_service.reject(proposal_id, payload.reason):
            raise HTTPException(status_code=409, detail="Proposal cannot be rejected in its current state.")
        return JSONResponse({"status": "rejected", "proposal_id": proposal_id})

    @app.get("/api/v1/admin/holdings")
    def admin_holdings(_: Dict[str, Any] = Depends(_admin_identity)) -> JSONResponse:
        return JSONResponse({"holdings": default_service.holdings(), "sector_weights": default_service.sector_weights()})

    @app.get("/api/v1/admin/risk")
    def admin_risk(_: Dict[str, Any] = Depends(_admin_identity)) -> JSONResponse:
        return JSONResponse(default_service.risk_profile() or {})

    @app.get("/api/v1/admin/performance")
    def admin_performance(_: Dict[str, Any] = Depends(_admin_identity)) -> JSONResponse:
        return JSONResponse({"report": default_service.performance(), "series": default_service.nav_series()})

    @app.get("/api/v1/admin/runs")
    def admin_runs(_: Dict[str, Any] = Depends(_admin_identity)) -> JSONResponse:
        return JSONResponse({"runs": default_service.runs()})

    @app.get("/api/v1/admin/operations")
    def admin_operations(_: Dict[str, Any] = Depends(_admin_identity)) -> JSONResponse:
        return JSONResponse(default_service.operations_overview())

    @app.get("/api/v1/admin/audit-events")
    def admin_audit_events(_: Dict[str, Any] = Depends(_admin_identity)) -> JSONResponse:
        return JSONResponse({"events": default_service.recent_decisions()})

    return app


app = create_app()


def serve() -> None:
    """Run the dashboard with uvicorn."""
    import uvicorn

    config = get_config()
    host = str(config.get("dashboard.host", "0.0.0.0"))
    port = int(config.get("dashboard.port", 8000) or 8000)
    logger.info("Dashboard starting on http://%s:%d", host, port)
    uvicorn.run(app, host=host, port=port, log_level="info")
