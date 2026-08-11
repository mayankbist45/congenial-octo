"""Shared helpers for journal repositories."""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any, Dict, Optional


def iso_date(value: Optional[date]) -> Optional[str]:
    return value.isoformat() if value else None


def iso_datetime(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat(timespec="seconds") if value else None


def decode_meta_decision(row: Dict[str, Any]) -> Dict[str, Any]:
    for key in ("features", "news_refs"):
        raw = row.get(key)
        if isinstance(raw, str) and raw:
            try:
                row[key] = json.loads(raw)
            except (TypeError, ValueError):
                row[key] = {} if key == "features" else []
    return row
