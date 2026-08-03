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
from typing import Any, Dict, Optional

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from src.config import Config, get_config
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
    service = DashboardService(config)

    app = FastAPI(title=service.fund_name, docs_url="/api/docs")
    templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
    templates.env.filters["money"] = money
    templates.env.filters["money_currency"] = format_money
    templates.env.filters["pct"] = pct
    templates.env.filters["number"] = number
    templates.env.filters["sign"] = sign_class
    templates.env.globals["region_label"] = region_label
    templates.env.globals["broker_label"] = broker_label

    def render(
        request: Request, template: str, context: Dict[str, Any]
    ) -> HTMLResponse:
        base = {
            "fund_name": service.fund_name,
            "benchmark_label": service.benchmark_label,
            "is_dry_run": service.is_dry_run,
            "pending_count": len(service.pending_proposals()),
        }
        base.update(context)
        return templates.TemplateResponse(request, template, base)

    # ------------------------------------------------------------------
    # Pages
    # ------------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    def overview(request: Request) -> HTMLResponse:
        series = service.nav_series()
        return render(
            request,
            "overview.html",
            {
                "page": "overview",
                "overview": service.overview(),
                "proposals": service.pending_proposals(),
                "performance": service.performance(),
                "chart": charts.line_chart(
                    series["dates"],
                    series["fund"],
                    series["benchmark"],
                    service.benchmark_label,
                ),
                "digest": service.digest(),
            },
        )

    @app.get("/proposals", response_class=HTMLResponse)
    def proposals(request: Request) -> HTMLResponse:
        return render(
            request,
            "proposals.html",
            {
                "page": "proposals",
                "proposals": service.pending_proposals(),
                "approved": service.approved_proposals(),
                "history": service.recent_decisions(),
            },
        )

    @app.get("/holdings", response_class=HTMLResponse)
    def holdings(request: Request) -> HTMLResponse:
        sectors = service.sector_weights()
        return render(
            request,
            "holdings.html",
            {
                "page": "holdings",
                "holdings": service.holdings(),
                "sectors": sectors,
                "sector_chart": charts.bar_chart(
                    [s["sector"] for s in sectors],
                    [s["weight_pct"] or 0.0 for s in sectors],
                ),
                "max_sector_pct": config.get("risk.max_sector_pct"),
                "max_position_pct": config.get("risk.max_position_pct"),
            },
        )

    @app.get("/news", response_class=HTMLResponse)
    def news(request: Request) -> HTMLResponse:
        return render(
            request, "news.html", {"page": "news", "digest": service.digest()}
        )

    @app.get("/performance", response_class=HTMLResponse)
    def performance(request: Request) -> HTMLResponse:
        series = service.nav_series()
        attribution = service.attribution()
        return render(
            request,
            "performance.html",
            {
                "page": "performance",
                "performance": service.performance(),
                "chart": charts.line_chart(
                    series["dates"],
                    series["fund"],
                    series["benchmark"],
                    service.benchmark_label,
                ),
                "attribution": attribution,
                "attribution_chart": charts.bar_chart(
                    [row["symbol"] for row in attribution[:15]],
                    [row["contribution_pct"] or 0.0 for row in attribution[:15]],
                ),
                "risk": service.risk_profile(),
            },
        )

    @app.get("/journal", response_class=HTMLResponse)
    def journal(request: Request) -> HTMLResponse:
        return render(
            request,
            "journal.html",
            {
                "page": "journal",
                "trades": service.trades(),
                "runs": service.runs(),
            },
        )

    @app.get("/journal/{run_id}", response_class=HTMLResponse)
    def run_detail(request: Request, run_id: int) -> HTMLResponse:
        run = service.run_detail(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Run not found")
        return render(
            request,
            "run.html",
            {
                "page": "journal",
                "run": run,
                "briefing": service.briefing(run["run_date"]),
            },
        )

    @app.get("/personas", response_class=HTMLResponse)
    def personas_page(request: Request) -> HTMLResponse:
        grouped = service.personas_by_region()
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
    def intraday_page(request: Request, region: Optional[str] = None) -> HTMLResponse:
        active = region.upper() if region else None
        if active and active not in {"IN", "US"}:
            raise HTTPException(status_code=400, detail="region must be IN or US")
        personas = service.intraday_personas(active)
        by_region = service.personas_by_region()
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
        )

    @app.get("/intraday/{persona_id}", response_class=HTMLResponse)
    def intraday_detail(request: Request, persona_id: str) -> HTMLResponse:
        from src.config import get_persona_config, persona_description

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
        )

    # ------------------------------------------------------------------
    # Approval actions
    # ------------------------------------------------------------------
    @app.post("/proposals/{proposal_id}/approve")
    def approve(proposal_id: int) -> RedirectResponse:
        if not service.approve(proposal_id):
            raise HTTPException(
                status_code=409, detail="Proposal is not pending a decision"
            )
        return RedirectResponse("/proposals", status_code=303)

    @app.post("/proposals/{proposal_id}/reject")
    def reject(proposal_id: int, reason: str = Form(default="")) -> RedirectResponse:
        if not service.reject(proposal_id, reason):
            raise HTTPException(
                status_code=409, detail="Proposal is not pending a decision"
            )
        return RedirectResponse("/proposals", status_code=303)

    @app.post("/proposals/approve-all")
    def approve_all() -> RedirectResponse:
        count = service.approve_all()
        logger.info("Approved %d proposal(s) in bulk", count)
        return RedirectResponse("/proposals", status_code=303)

    @app.post("/proposals/reject-all")
    def reject_all() -> RedirectResponse:
        count = service.reject_all()
        logger.info("Rejected %d proposal(s) in bulk", count)
        return RedirectResponse("/proposals", status_code=303)

    # ------------------------------------------------------------------
    # JSON API, for scripts and the orchestrator
    # ------------------------------------------------------------------
    @app.get("/api/overview")
    def api_overview() -> JSONResponse:
        return JSONResponse(service.overview())

    @app.get("/api/holdings")
    def api_holdings() -> JSONResponse:
        return JSONResponse(service.holdings())

    @app.get("/api/proposals")
    def api_proposals() -> JSONResponse:
        return JSONResponse(
            {
                "pending": service.pending_proposals(),
                "approved": service.approved_proposals(),
            }
        )

    @app.get("/api/performance")
    def api_performance() -> JSONResponse:
        return JSONResponse(
            {"report": service.performance(), "series": service.nav_series()}
        )

    @app.get("/api/risk")
    def api_risk() -> JSONResponse:
        return JSONResponse(service.risk_profile() or {})

    @app.get("/api/personas")
    def api_personas() -> JSONResponse:
        return JSONResponse(service.personas_by_region())

    @app.get("/api/intraday")
    def api_intraday(region: Optional[str] = None) -> JSONResponse:
        active = region.upper() if region else None
        return JSONResponse(service.intraday_personas(active))

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
