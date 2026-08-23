"""Immutable delayed/EOD read model for the separately deployed React UI."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Any, Dict, Optional

from src.config import Config, get_config
from src.dashboard.service import DashboardService
from src.journal.db import connect

DISCLAIMER = "Research workspace. Not investment advice. No live order execution."


class DashboardSnapshotService:
    def __init__(self, config: Optional[Config] = None):
        self._config = config or get_config()

    def publish(self, snapshot_date: Optional[date] = None) -> Dict[str, Any]:
        day = (snapshot_date or date.today()).isoformat()
        existing = self.for_date(day)
        if existing is not None:
            return existing

        dashboard = DashboardService(self._config)
        overview = dashboard.overview()
        payload = {
            "as_of": datetime.now(timezone.utc).isoformat(),
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
            "disclaimer": DISCLAIMER,
        }
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

    def latest(self) -> Optional[Dict[str, Any]]:
        conn = connect(self._config)
        try:
            row = conn.execute(
                "SELECT payload FROM dashboard_snapshots ORDER BY snapshot_date DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        return json.loads(row["payload"]) if row else None

    def for_date(self, snapshot_date: str) -> Optional[Dict[str, Any]]:
        conn = connect(self._config)
        try:
            row = conn.execute(
                "SELECT payload FROM dashboard_snapshots WHERE snapshot_date = ?", (snapshot_date,)
            ).fetchone()
        finally:
            conn.close()
        return json.loads(row["payload"]) if row else None
