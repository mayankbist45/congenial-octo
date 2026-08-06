"""FastAPI dashboard.

The approval gate. Nothing the fund proposes reaches the broker until a human
approves it here, so the pages are built around one question: is there enough
information on screen to say yes or no responsibly? Each proposal shows the
trigger that produced it, the written rationale, the risk warnings attached
during validation, and the resulting position size.

The dashboard binds to localhost by default and has no authentication, because it
is a single-operator tool for a personal fund. Do not expose it to a network.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, Form, HTTPException, Query, Request
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

logger = get_logger(__name__)

TEMPLATE_DIR = Path(__file__).parent / "templates"


def create_app(config: Optional[Config] = None) -> FastAPI:
    config = config or get_config()
    default_service = DashboardService(config)

    app = FastAPI(title=default_service.fund_name, docs_url="/api/docs")
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

    def _get_service(persona: Optional[str] = None) -> DashboardService:
        if not persona:
            return default_service
        if persona not in _persona_services:
            pcfg = get_persona_config(persona, base=config)
            _persona_services[persona] = DashboardService(config=pcfg)
        return _persona_services[persona]

    def _persona_nav_items() -> List[Dict[str, str]]:
        return [
            {"id": pid, "description": persona_description(pid)}
            for pid in enabled_personas()
        ]

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
        base = {
            "fund_name": header_fund_name or svc.fund_name,
            "benchmark_label": svc.benchmark_label,
            "is_dry_run": svc.is_dry_run,
            "pending_count": pending_count,
            "current_persona": persona_id,
            "persona_description": persona_description(persona_id),
            "personas": _persona_nav_items(),
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
            chart_html = charts.line_chart([], [], [], svc.benchmark_label)
        elif is_intraday:
            overview_data = {"has_data": False, "is_intraday": True}
            performance_data = {
                "total_return_pct": None,
                "excess_return_pct": None,
                "benchmark_return_pct": None,
            }
            chart_html = charts.line_chart([], [], [], svc.benchmark_label)
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

        intraday_books = (
            []
            if is_intraday
            else default_service.intraday_personas()
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
                "intraday_books": intraday_books,
                "region_labels": {"IN": region_label("IN"), "US": region_label("US")},
            },
            service=svc,
        )

    @app.get("/proposals", response_class=HTMLResponse)
    def proposals(
        request: Request, persona: Optional[str] = Query(default=None)
    ) -> HTMLResponse:
        svc, pcfg, is_intraday, intraday_summary = _persona_context(persona)
        return render(
            request,
            "proposals.html",
            {
                "page": "proposals",
                "proposals": [] if is_intraday else svc.pending_proposals(),
                "approved": [] if is_intraday else svc.approved_proposals(),
                "history": [] if is_intraday else svc.recent_decisions(),
                "mandate": svc.mandate_summary(),
                "analytics": svc.analytics_summary(),
                "intraday_summary": intraday_summary,
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
        return render(
            request,
            "holdings.html",
            {
                "page": "holdings",
                "holdings": holdings_rows,
                "open_positions": open_positions,
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
            chart_html = charts.line_chart([], [], [], svc.benchmark_label)
            attribution: List[Dict[str, Any]] = []
            attribution_chart = ""
            risk = None
        elif is_intraday:
            perf = {"is_intraday": True, "start_date": None, "end_date": None}
            chart_html = charts.line_chart([], [], [], svc.benchmark_label)
            attribution = []
            attribution_chart = ""
            risk = None
        else:
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
            },
            service=svc,
        )

    @app.get("/journal", response_class=HTMLResponse)
    def journal(
        request: Request, persona: Optional[str] = Query(default=None)
    ) -> HTMLResponse:
        svc = _get_service(persona)
        return render(
            request,
            "journal.html",
            {
                "page": "journal",
                "trades": svc.trades(),
                "runs": svc.runs(),
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
    # JSON API, for scripts and the orchestrator
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

    return app


app = create_app()


def serve() -> None:
    """Run the dashboard with uvicorn."""
    import uvicorn

    config = get_config()
    host = str(config.get("dashboard.host", "127.0.0.1"))
    port = int(config.get("dashboard.port", 8000) or 8000)
    logger.info("Dashboard starting on http://%s:%d", host, port)
    uvicorn.run(app, host=host, port=port, log_level="info")
