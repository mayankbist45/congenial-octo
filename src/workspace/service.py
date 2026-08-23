"""Delayed-data research workspace services.

This module deliberately produces reviewable research snapshots only. It has no
dependency on the execution layer and never emits broker instructions.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src.config import Config, get_config
from src.fundamentals.service import FundamentalsService
from src.journal.db import connect
from src.market_data.prices import PriceService
from src.quant.scoring import ScoringEngine
from src.risk_analytics.market_regime import assess_market_regime
from src.universe import Universe, load_universe

DISCLAIMER = "Research workspace. Not investment advice. No live order execution."
DEFAULT_PROFILE = {
    "horizon": "1-3 years",
    "risk_tolerance": "moderate",
    "target_positions": 15,
    "market_cap": "mid",
    "included_sectors": [],
    "excluded_sectors": [],
    "factor_weights": {"quality": 30, "value": 25, "growth": 20, "momentum": 15, "trend": 10},
    "max_debt_to_equity": 2.0,
    "minimum_liquidity_inr": 50_000_000,
    "include_ipos": False,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _loads(value: Any, default: Any) -> Any:
    try:
        return json.loads(value) if value else default
    except (TypeError, ValueError):
        return default


class WorkspaceAccessError(PermissionError):
    """Raised when an authenticated identity does not have an active invite."""


class WorkspaceRepository:
    def __init__(self, config: Optional[Config] = None):
        self._config = config or get_config()

    def user_for_email(self, email: str) -> Dict[str, Any]:
        email = email.strip().lower()
        with self._connection() as conn:
            invite = conn.execute(
                "SELECT * FROM workspace_invites WHERE email = ?", (email,)
            ).fetchone()
            if not invite or invite["status"] != "active":
                raise WorkspaceAccessError("This Google account does not have an active workspace invite.")
            now = _now()
            conn.execute(
                "INSERT INTO workspace_users (email, role, created_at, last_seen_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(email) DO UPDATE SET last_seen_at = excluded.last_seen_at",
                (email, invite["role"], now, now),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM workspace_users WHERE email = ?", (email,)).fetchone()
            return dict(row)

    def create_invite(self, email: str, role: str = "member", created_by: str = "system") -> None:
        if role not in {"admin", "member"}:
            raise ValueError("Workspace role must be admin or member.")
        with self._connection() as conn:
            conn.execute(
                "INSERT INTO workspace_invites (email, role, status, created_at, created_by) VALUES (?, ?, 'active', ?, ?) "
                "ON CONFLICT(email) DO UPDATE SET role = excluded.role, status = 'active'",
                (email.strip().lower(), role, _now(), created_by),
            )
            conn.commit()

    def profile(self, user_id: int) -> Dict[str, Any]:
        with self._connection() as conn:
            row = conn.execute("SELECT payload FROM workspace_profiles WHERE user_id = ?", (user_id,)).fetchone()
        stored = _loads(row["payload"], {}) if row else {}
        return {**DEFAULT_PROFILE, **stored}

    def save_profile(self, user_id: int, profile: Dict[str, Any]) -> Dict[str, Any]:
        payload = {**DEFAULT_PROFILE, **profile}
        with self._connection() as conn:
            conn.execute(
                "INSERT INTO workspace_profiles (user_id, payload, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET payload = excluded.payload, updated_at = excluded.updated_at",
                (user_id, json.dumps(payload), _now()),
            )
            conn.commit()
        return payload

    def create_job(self, user_id: int) -> Dict[str, Any]:
        job_id, now = str(uuid.uuid4()), _now()
        with self._connection() as conn:
            conn.execute(
                "INSERT INTO workspace_jobs (id, user_id, status, stage, progress, warnings, created_at, updated_at) "
                "VALUES (?, ?, 'queued', 'queued', 0, '[]', ?, ?)",
                (job_id, user_id, now, now),
            )
            conn.commit()
        return self.job(job_id, user_id)

    def job(self, job_id: str, user_id: int) -> Dict[str, Any]:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM workspace_jobs WHERE id = ? AND user_id = ?", (job_id, user_id)).fetchone()
        if not row:
            raise KeyError("Workspace job not found.")
        out = dict(row)
        out["warnings"] = _loads(out.pop("warnings"), [])
        return out

    def update_job(self, job_id: str, **updates: Any) -> None:
        allowed = {"status", "stage", "progress", "warnings", "error", "portfolio_id"}
        fields = [(key, value) for key, value in updates.items() if key in allowed]
        if not fields:
            return
        assignments = ", ".join(f"{key} = ?" for key, _ in fields) + ", updated_at = ?"
        values = [json.dumps(value) if key == "warnings" else value for key, value in fields] + [_now(), job_id]
        with self._connection() as conn:
            conn.execute(f"UPDATE workspace_jobs SET {assignments} WHERE id = ?", values)
            conn.commit()

    def save_portfolio(self, user_id: int, payload: Dict[str, Any]) -> str:
        portfolio_id = str(uuid.uuid4())
        with self._connection() as conn:
            row = conn.execute("SELECT COALESCE(MAX(version), 0) AS version FROM workspace_portfolios WHERE user_id = ?", (user_id,)).fetchone()
            version = int(row["version"]) + 1
            conn.execute(
                "INSERT INTO workspace_portfolios (id, user_id, version, as_of, payload, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (portfolio_id, user_id, version, payload["as_of"], json.dumps(payload), _now()),
            )
            conn.commit()
        return portfolio_id

    def portfolio(self, portfolio_id: str, user_id: int) -> Dict[str, Any]:
        with self._connection() as conn:
            row = conn.execute("SELECT payload FROM workspace_portfolios WHERE id = ? AND user_id = ?", (portfolio_id, user_id)).fetchone()
        if not row:
            raise KeyError("Portfolio not found.")
        return _loads(row["payload"], {})

    def latest_portfolio(self, user_id: int) -> Optional[Dict[str, Any]]:
        with self._connection() as conn:
            row = conn.execute("SELECT payload FROM workspace_portfolios WHERE user_id = ? ORDER BY version DESC LIMIT 1", (user_id,)).fetchone()
        return _loads(row["payload"], None) if row else None

    def inbox(self, user_id: int) -> List[Dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute("SELECT * FROM workspace_events WHERE user_id = ? ORDER BY created_at DESC", (user_id,)).fetchall()
        return [{**_loads(row["payload"], {}), "id": row["id"], "acknowledged_at": row["acknowledged_at"]} for row in rows]

    def add_event(self, user_id: int, payload: Dict[str, Any]) -> str:
        event_id = str(uuid.uuid4())
        with self._connection() as conn:
            conn.execute(
                "INSERT INTO workspace_events (id, user_id, payload, created_at) VALUES (?, ?, ?, ?)",
                (event_id, user_id, json.dumps(payload), _now()),
            )
            conn.commit()
        return event_id

    def acknowledge_event(self, event_id: str, user_id: int) -> None:
        with self._connection() as conn:
            cursor = conn.execute("UPDATE workspace_events SET acknowledged_at = ? WHERE id = ? AND user_id = ?", (_now(), event_id, user_id))
            conn.commit()
        if cursor.rowcount == 0:
            raise KeyError("Workspace event not found.")

    class _ConnectionContext:
        def __init__(self, config: Config): self.conn = connect(config)
        def __enter__(self): return self.conn
        def __exit__(self, *_): self.conn.close()

    def _connection(self):
        return self._ConnectionContext(self._config)


class WorkspaceService:
    def __init__(self, config: Optional[Config] = None, universe: Optional[Universe] = None):
        self._config = config or get_config()
        self.repo = WorkspaceRepository(self._config)
        self._universe = universe or load_universe(self._config)

    def generate(self, job_id: str, user_id: int) -> None:
        self.repo.update_job(job_id, status="running", stage="screening_universe", progress=10)
        profile = self.repo.profile(user_id)
        try:
            symbols = self._universe.symbols
            prices = PriceService(self._config)
            technicals = prices.snapshots(symbols)
            liquid = [symbol for symbol, item in technicals.items() if (item.avg_daily_turnover or 0) >= float(profile["minimum_liquidity_inr"])]
            self.repo.update_job(job_id, stage="reading_fundamentals", progress=35)
            fundamentals = FundamentalsService(self._config).get_many(liquid)
            candidates = {symbol: item for symbol, item in fundamentals.items() if item.market_cap_bucket == "mid"}
            if len(candidates) < 12:
                candidates = fundamentals
            self.repo.update_job(job_id, stage="scoring_research", progress=60)
            scores = ScoringEngine(self._config, self._universe).score(candidates, {symbol: technicals[symbol] for symbol in candidates if symbol in technicals})
            selected = self._select(scores, candidates, technicals, profile)
            self.repo.update_job(job_id, stage="assessing_market_regime", progress=80)
            benchmark = prices.close_series(str(self._config.get("fund.benchmark_symbol", "^CRSLDX")))
            frames = prices.load_history([item["symbol"] for item in selected])
            regime = assess_market_regime(benchmark, {symbol: frame["Close"] for symbol, frame in frames.items()}, technicals)
            payload = self._portfolio_payload(selected, regime, profile)
            portfolio_id = self.repo.save_portfolio(user_id, payload)
            for holding in selected[:3]:
                self.repo.add_event(user_id, {
                    "type": "portfolio_candidate", "impact": "medium", "symbol": holding["symbol"],
                    "title": f"{holding['company_name']} entered the model portfolio",
                    "detail": "The name passed the current research, liquidity, and diversification checks.",
                    "created_at": _now(),
                })
            self.repo.update_job(job_id, status="completed", stage="completed", progress=100, portfolio_id=portfolio_id)
        except Exception as exc:
            self.repo.update_job(job_id, status="failed", stage="failed", error=str(exc))

    def _select(self, scores, fundamentals, technicals, profile):
        limit = min(max(int(profile.get("target_positions", 15)), 12), 20)
        sectors, result = set(profile.get("excluded_sectors") or []), []
        sector_counts: Dict[str, int] = {}
        for score in sorted(scores.values(), key=lambda item: item.composite, reverse=True):
            if not score.passes_gates or score.symbol not in fundamentals:
                continue
            entry = self._universe.get(score.symbol)
            if not entry or entry.sector in sectors or sector_counts.get(entry.sector, 0) >= 3:
                continue
            item = technicals.get(score.symbol)
            if not item or item.last_price is None:
                continue
            sector_counts[entry.sector] = sector_counts.get(entry.sector, 0) + 1
            result.append({"symbol": score.symbol, "company_name": entry.company_name, "sector": entry.sector, "score": score, "fundamental": fundamentals[score.symbol], "technical": item})
            if len(result) == limit:
                break
        if len(result) < 12:
            raise RuntimeError("Insufficient eligible, liquid research coverage to build a model portfolio.")
        return result

    def _portfolio_payload(self, selected, regime, profile):
        weight = round(95.0 / len(selected), 2)
        holdings = []
        for item in selected:
            score, fundamental, technical = item["score"], item["fundamental"], item["technical"]
            holdings.append({
                "symbol": item["symbol"], "company_name": item["company_name"], "sector": item["sector"], "weight_pct": weight,
                "price": technical.last_price, "return_1m": technical.return_1m, "return_12m": technical.return_12m,
                "research_strength": round(float(score.percentile or 0), 1), "composite_score": round(float(score.composite), 3),
                "evidence_coverage": round(float(score.metric_coverage_pct or 0), 1), "pe_ratio": fundamental.pe_ratio,
                "roe_pct": fundamental.roe_pct, "debt_to_equity": fundamental.debt_to_equity,
                "reason": "Eligible after quality, valuation, growth, momentum, trend, liquidity, and diversification checks.",
            })
        return {
            "as_of": _now(), "data_status": "delayed_eod", "disclaimer": DISCLAIMER, "profile": profile,
            "cash_pct": 5.0, "holdings": holdings,
            "market_regime": {"label": regime.regime, "score": regime.score, "evidence": regime.evidence, "warnings": regime.warnings},
            "constraints": {"holding_count": len(holdings), "max_sector_positions": 3, "cash_buffer_pct": 5.0},
        }
