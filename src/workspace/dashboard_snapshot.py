"""Immutable delayed/EOD read model for the separately deployed React UI."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Any, Dict, Optional

from src.config import Config, get_config, get_persona_config
from src.dashboard.service import DashboardService
from src.journal.db import connect

DISCLAIMER = "Research workspace. Not investment advice. No live order execution."


class DashboardSnapshotService:
    def __init__(self, config: Optional[Config] = None):
        self._config = config or get_config()

    def publish(self, snapshot_date: Optional[date] = None, ipos: Optional[Dict[str, Any]] = None, refresh_ipos: bool = False, refresh_portfolio: bool = False) -> Dict[str, Any]:
        day = (snapshot_date or date.today()).isoformat()
        existing = self.for_date(day)
        if existing is not None:
            if refresh_portfolio:
                return self.refresh_portfolio(snapshot_date, ipos=ipos)
            if refresh_ipos and ipos is not None:
                refreshed = dict(existing)
                refreshed["ipos"] = ipos
                conn = connect(self._config)
                try:
                    conn.execute(
                        "UPDATE dashboard_snapshots SET payload = ? WHERE snapshot_date = ?",
                        (json.dumps(refreshed), day),
                    )
                    conn.commit()
                finally:
                    conn.close()
                return refreshed
            if "personas" in existing and (ipos is None or "ipos" in existing):
                return existing
            # A pre-persona snapshot is a legacy read model, not a different
            # market observation. Upgrade its shape once while preserving its
            # original publication timestamp and date.
            upgraded = self._build_payload(day, as_of=existing["as_of"], ipos=ipos)
            conn = connect(self._config)
            try:
                conn.execute(
                    "UPDATE dashboard_snapshots SET payload = ? WHERE snapshot_date = ?",
                    (json.dumps(upgraded), day),
                )
                conn.commit()
            finally:
                conn.close()
            return upgraded

        payload = self._build_payload(day, ipos=ipos)
        conn = connect(self._config)
        try:
            conn.execute(
                "INSERT INTO dashboard_snapshots (snapshot_date, as_of, payload, created_at) VALUES (?, ?, ?, ?)",
                (day, payload["as_of"], json.dumps(payload), payload["as_of"]),
            )
            conn.commit()
        finally:
            conn.close()
        return payload

    def refresh_portfolio(self, snapshot_date: Optional[date] = None, ipos: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Publish current paper holdings without dropping today's IPO research."""
        day = (snapshot_date or date.today()).isoformat()
        existing = self.for_date(day)
        payload = self._build_payload(day, ipos=ipos if ipos is not None else (existing or {}).get("ipos"))
        conn = connect(self._config)
        try:
            if existing is None:
                conn.execute(
                    "INSERT INTO dashboard_snapshots (snapshot_date, as_of, payload, created_at) VALUES (?, ?, ?, ?)",
                    (day, payload["as_of"], json.dumps(payload), payload["as_of"]),
                )
            else:
                conn.execute(
                    "UPDATE dashboard_snapshots SET as_of = ?, payload = ? WHERE snapshot_date = ?",
                    (payload["as_of"], json.dumps(payload), day),
                )
            conn.commit()
        finally:
            conn.close()
        return payload

    def _build_payload(self, day: str, as_of: Optional[str] = None, ipos: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        dashboard = DashboardService(self._config)
        overview = dashboard.overview()
        personas = self._persona_payloads(dashboard)
        return {
            "as_of": as_of or datetime.now(timezone.utc).isoformat(),
            "snapshot_date": day,
            "data_status": "delayed_eod",
            "overview": overview,
            "holdings": dashboard.holdings(),
            "sector_weights": dashboard.sector_weights(),
            "performance": dashboard.performance(),
            "risk": dashboard.risk_profile() or {},
            "source_freshness": {
                "prices": overview.get("latest_nav_date"),
                "analysis": overview.get("latest_analysis_date"),
            },
            "warnings": overview.get("warnings") or [],
            # The original fields above intentionally remain for compatibility.
            # New workspace readers consume these immutable per-persona records.
            "personas": personas,
            "ipos": ipos or {"issues": [], "warnings": [], "data_status": "unavailable"},
            "disclaimer": DISCLAIMER,
        }

    def _persona_payloads(self, dashboard: DashboardService) -> list[Dict[str, Any]]:
        """Capture every enabled paper persona at one EOD publication boundary."""
        metadata = {
            row["persona_id"]: row
            for row in dashboard.all_personas_overview()
        }
        personas: list[Dict[str, Any]] = []
        configured = self._config.raw.get("personas", {})
        if not isinstance(configured, dict):
            configured = {}

        for persona_id, definition in configured.items():
            if not isinstance(definition, dict) or not definition.get("enabled", True):
                continue
            service = DashboardService(get_persona_config(persona_id, base=self._config))
            intraday = service.intraday_summary()
            is_intraday = service.is_intraday
            overview = (
                service.intraday_dashboard_overview(intraday)
                if is_intraday and intraday
                else service.overview()
            )
            performance = (
                service.intraday_performance(intraday)
                if is_intraday and intraday
                else service.performance()
            )
            news = (
                service.intraday_news_context(intraday)
                if is_intraday
                else service.digest()
            )
            meta = metadata.get(persona_id, {})
            warnings = list(overview.get("warnings") or [])
            if is_intraday and not intraday:
                warnings.append("No intraday scan summary has been published for this persona.")
            personas.append({
                "id": persona_id,
                "metadata": meta,
                "overview": overview,
                "holdings": service.holdings(),
                "sector_weights": service.sector_weights(),
                "performance": performance,
                "performance_series": service.nav_series() if not is_intraday else {},
                "risk": service.risk_profile() or {},
                "runs": service.runs(),
                "news": news or {"has_data": False},
                "intraday": intraday if is_intraday else None,
                "warnings": warnings,
                "source_freshness": {
                    "prices": overview.get("latest_nav_date") or (intraday or {}).get("end_date"),
                    "analysis": overview.get("latest_analysis_date") or (news or {}).get("run_date"),
                    "news": (news or {}).get("run_date"),
                },
            })
        return personas

    def latest(self) -> Optional[Dict[str, Any]]:
        conn = connect(self._config)
        try:
            row = conn.execute(
                "SELECT payload FROM dashboard_snapshots ORDER BY snapshot_date DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        return self._with_status(json.loads(row["payload"])) if row else None

    def for_date(self, snapshot_date: str) -> Optional[Dict[str, Any]]:
        conn = connect(self._config)
        try:
            row = conn.execute(
                "SELECT payload FROM dashboard_snapshots WHERE snapshot_date = ?", (snapshot_date,)
            ).fetchone()
        finally:
            conn.close()
        return json.loads(row["payload"]) if row else None

    @staticmethod
    def _with_status(snapshot: Dict[str, Any]) -> Dict[str, Any]:
        """Mark an old successful snapshot stale without mutating stored history."""
        copy = dict(snapshot)
        try:
            published_day = date.fromisoformat(str(copy["snapshot_date"]))
        except (KeyError, ValueError):
            return copy
        # Weekends do not require a new Indian EOD publication. Three days also
        # covers a normal Friday snapshot viewed on Monday before the close run.
        if (date.today() - published_day).days > 3:
            copy["data_status"] = "stale_eod"
            copy["warnings"] = list(copy.get("warnings") or []) + [
                "Showing the last successful publication; a newer snapshot is unavailable."
            ]
        return copy
