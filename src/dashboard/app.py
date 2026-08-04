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
from typing import Any, Dict, List, Optional

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
from src.dashboard.service import (
    DashboardService,
    broker_label,
    format_money,
    money,
    number,
    pct,
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

    def render(
        request: Request,
        template: str,
        context: Dict[str, Any],
        service: Optional[DashboardService] = None,
    ) -> HTMLResponse:
        svc = service or default_service
        try:
            pending_count = len(svc.pending_proposals())
        except Exception:
            pending_count = 0
        persona_id = svc.persona_id
        base = {
            "fund_name": svc.fund_name,
            "benchmark_label": svc.benchmark_label,
            "is_dry_run": svc.is_dry_run,
            "pending_count": pending_count,
            "current_persona": persona_id,
            "persona_description": persona_description(persona_id),
            "personas": _persona_nav_items(),
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
        svc = _get_service(persona)
        series = svc.nav_series()
        return render(
            request,
            "overview.html",
            {
                "page": "overview",
                "overview": svc.overview(),
                "proposals": svc.pending_proposals(),
                "performance": svc.performance(),
                "chart": charts.line_chart(
                    series["dates"],
                    series["fund"],
                    series["benchmark"],
                    svc.benchmark_label,
                ),
                "digest": svc.digest(),
            },
            service=svc,
        )

    @app.get("/proposals", response_class=HTMLResponse)
    def proposals(
        request: Request, persona: Optional[str] = Query(default=None)
    ) -> HTMLResponse:
        svc = _get_service(persona)
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
            },
            service=svc,
        )

    @app.get("/holdings", response_class=HTMLResponse)
    def holdings(
        request: Request, persona: Optional[str] = Query(default=None)
    ) -> HTMLResponse:
        svc = _get_service(persona)
        sectors = svc.sector_weights()
        pcfg = get_persona_config(persona, base=config) if persona else config
        return render(
            request,
            "holdings.html",
            {
                "page": "holdings",
                "holdings": svc.holdings(),
                "sectors": sectors,
                "sector_chart": charts.bar_chart(
                    [s["sector"] for s in sectors],
                    [s["weight_pct"] or 0.0 for s in sectors],
                ),
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
        svc = _get_service(persona)
        return render(
            request, "news.html", {"page": "news", "digest": svc.digest()}, service=svc
        )

    @app.get("/performance", response_class=HTMLResponse)
    def performance(
        request: Request, persona: Optional[str] = Query(default=None)
    ) -> HTMLResponse:
        svc = _get_service(persona)
        series = svc.nav_series()
        attribution = svc.attribution()
        return render(
            request,
            "performance.html",
            {
                "page": "performance",
                "performance": svc.performance(),
                "chart": charts.line_chart(
                    series["dates"],
                    series["fund"],
                    series["benchmark"],
                    svc.benchmark_label,
                ),
                "attribution": attribution,
                "attribution_chart": charts.bar_chart(
                    [row["symbol"] for row in attribution[:15]],
                    [row["contribution_pct"] or 0.0 for row in attribution[:15]],
                ),
                "risk": svc.risk_profile(),
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

    @app.get("/personas", response_class=HTMLResponse)
    def personas_page(request: Request) -> HTMLResponse:
        grouped = default_service.personas_by_region()
        return render(
            request,
            "personas.html",
            {
                "page": "personas",
                "regions": grouped,
                "region_labels": {
                    "IN": region_label("IN"),
                    "US": region_label("US"),
                },
            },
        )

    @app.get("/intraday", response_class=HTMLResponse)
    def intraday_page(
        request: Request,
        region: Optional[str] = None,
        persona: Optional[str] = Query(default=None),
    ) -> HTMLResponse:
        active = region.upper() if region else None
        if active and active not in {"IN", "US"}:
            raise HTTPException(status_code=400, detail="region must be IN or US")
        personas = default_service.intraday_personas(active)
        by_region = default_service.personas_by_region()
        return render(
            request,
            "intraday.html",
            {
                "page": "intraday",
                "personas": personas,
                "intraday_by_region": {
                    "IN": [p for p in by_region.get("IN", []) if p.get("intraday")],
                    "US": [p for p in by_region.get("US", []) if p.get("intraday")],
                },
                "active_region": active,
                "region_labels": {
                    "IN": region_label("IN"),
                    "US": region_label("US"),
                },
            },
            service=_get_service(persona),
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
        currency = str(pcfg.get("fund.base_currency", "INR" if region == "IN" else "USD"))
        capital = pcfg.secrets.initial_capital
        persona = {
            "persona_id": persona_id,
            "description": persona_description(persona_id),
            "region": region,
            "region_label": region_label(region),
            "currency": currency,
            "capital": capital,
            "broker_label": broker_label(pcfg.broker_provider),
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
        return JSONResponse(default_service.personas_by_region())

    @app.get("/api/intraday")
    def api_intraday(region: Optional[str] = None) -> JSONResponse:
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
