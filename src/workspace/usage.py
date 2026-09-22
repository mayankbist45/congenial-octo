"""Rolling usage counters for subscription quotas."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from src.config import Config, get_config
from src.journal.db import connect


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _month_start(now: Optional[datetime] = None) -> str:
    current = now or _now()
    return datetime(current.year, current.month, 1, tzinfo=timezone.utc).isoformat()


def _day_start(now: Optional[datetime] = None) -> str:
    current = now or _now()
    return datetime(current.year, current.month, current.day, tzinfo=timezone.utc).isoformat()


def _hour_start(now: Optional[datetime] = None) -> str:
    current = now or _now()
    return datetime(
        current.year, current.month, current.day, current.hour, tzinfo=timezone.utc
    ).isoformat()


PERIOD_FOR_QUOTA = {
    "agent_runs": "month",
    "agent_ic_memo": "month",
    "holding_intel": "day",
    "manual_refresh": "month",
    "dashboard_reads": "hour",
}


def period_start_for(quota_key: str, now: Optional[datetime] = None) -> str:
    period = PERIOD_FOR_QUOTA.get(quota_key, "month")
    if period == "day":
        return _day_start(now)
    if period == "hour":
        return _hour_start(now)
    return _month_start(now)


def resets_at_for(quota_key: str, now: Optional[datetime] = None) -> Optional[str]:
    if quota_key not in PERIOD_FOR_QUOTA:
        return None
    current = now or _now()
    period = PERIOD_FOR_QUOTA[quota_key]
    if period == "month":
        if current.month == 12:
            nxt = datetime(current.year + 1, 1, 1, tzinfo=timezone.utc)
        else:
            nxt = datetime(current.year, current.month + 1, 1, tzinfo=timezone.utc)
        return nxt.isoformat()
    if period == "day":
        from datetime import timedelta

        nxt = datetime(current.year, current.month, current.day, tzinfo=timezone.utc) + timedelta(days=1)
        return nxt.isoformat()
    if period == "hour":
        from datetime import timedelta

        start = datetime(
            current.year, current.month, current.day, current.hour, tzinfo=timezone.utc
        )
        return (start.replace() + timedelta(hours=1)).isoformat()
    return None


class UsageCounter:
    def __init__(self, config: Optional[Config] = None):
        self._config = config or get_config()

    def get_used(self, subscription_id: str, quota_key: str) -> int:
        period_start = period_start_for(quota_key)
        conn = connect(self._config)
        try:
            row = conn.execute(
                "SELECT used FROM workspace_usage_counters "
                "WHERE subscription_id = ? AND quota_key = ? AND period_start = ?",
                (subscription_id, quota_key, period_start),
            ).fetchone()
            return int(row["used"]) if row else 0
        finally:
            conn.close()

    def consume(self, subscription_id: str, quota_key: str, amount: int = 1) -> int:
        period_start = period_start_for(quota_key)
        conn = connect(self._config)
        try:
            row = conn.execute(
                "SELECT used FROM workspace_usage_counters "
                "WHERE subscription_id = ? AND quota_key = ? AND period_start = ?",
                (subscription_id, quota_key, period_start),
            ).fetchone()
            used = int(row["used"]) + amount if row else amount
            if row:
                conn.execute(
                    "UPDATE workspace_usage_counters SET used = ? "
                    "WHERE subscription_id = ? AND quota_key = ? AND period_start = ?",
                    (used, subscription_id, quota_key, period_start),
                )
            else:
                conn.execute(
                    "INSERT INTO workspace_usage_counters "
                    "(subscription_id, quota_key, period_start, used) VALUES (?, ?, ?, ?)",
                    (subscription_id, quota_key, period_start, used),
                )
            conn.commit()
            return used
        finally:
            conn.close()

    def snapshot(self, subscription_id: str, quota_key: str, limit: int) -> Dict[str, Any]:
        used = self.get_used(subscription_id, quota_key)
        return {
            "used": used,
            "limit": limit,
            "resets_at": resets_at_for(quota_key),
        }
