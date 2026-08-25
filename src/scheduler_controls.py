"""Durable enable switches for scheduled work, shared by the API and cron."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional

from src.config import Config, get_config
from src.journal.db import session
from src.logging_setup import get_logger

logger = get_logger(__name__)

CONTROLS: Dict[str, Dict[str, str]] = {
    "india_long_cycle": {"label": "India long-horizon cycle", "schedule": "Weekdays 08:15 IST"},
    "us_long_cycle": {"label": "US long-horizon cycle", "schedule": "Weekdays 08:15 IST"},
    "india_intraday": {"label": "India intraday watches", "schedule": "Weekdays 09:10-15:35 IST"},
    "us_intraday": {"label": "US intraday watches", "schedule": "Weekdays 18:50-01:45 IST"},
    "weekly_research": {"label": "Weekly research", "schedule": "Saturday 09:00 IST"},
    "close": {"label": "Close, publication, and digest", "schedule": "Weekdays 18:00 IST"},
    "backup": {"label": "Journal backup", "schedule": "Weekdays 19:30 IST"},
}


class SchedulerControls:
    def __init__(self, config: Optional[Config] = None):
        self._config = config or get_config()

    def all(self) -> list[Dict[str, Any]]:
        try:
            with session(self._config) as conn:
                rows = {row["control_key"]: dict(row) for row in conn.execute("SELECT * FROM scheduler_controls").fetchall()}
        except Exception as exc:
            logger.warning("Scheduler controls unavailable; allowing scheduled work: %s", exc)
            rows = {}
        return [self._row(key, rows.get(key)) for key in CONTROLS]

    def enabled(self, key: str) -> bool:
        return next((bool(row["enabled"]) for row in self.all() if row["key"] == key), True)

    def set(self, key: str, enabled: bool, updated_by: str) -> Dict[str, Any]:
        if key not in CONTROLS:
            raise ValueError(f"Unknown scheduler control: {key}")
        now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        with session(self._config) as conn:
            conn.execute(
                """
                INSERT INTO scheduler_controls (control_key, enabled, updated_at, updated_by)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(control_key) DO UPDATE SET
                    enabled = excluded.enabled, updated_at = excluded.updated_at, updated_by = excluded.updated_by
                """,
                (key, int(enabled), now, updated_by),
            )
        return self._row(key, {"enabled": int(enabled), "updated_at": now, "updated_by": updated_by})

    @staticmethod
    def _row(key: str, row: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        definition = CONTROLS[key]
        return {
            "key": key,
            "label": definition["label"],
            "expected_schedule": definition["schedule"],
            "enabled": bool((row or {}).get("enabled", 1)),
            "updated_at": (row or {}).get("updated_at"),
            "updated_by": (row or {}).get("updated_by"),
        }


def _main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Read scheduler control state")
    parser.add_argument("command", choices=("enabled",))
    parser.add_argument("key", choices=tuple(CONTROLS))
    args = parser.parse_args(list(argv) if argv is not None else None)
    print("1" if SchedulerControls().enabled(args.key) else "0")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
