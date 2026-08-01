"""Aggregate reviewed decisions into advisory lessons."""

from __future__ import annotations

import json
from collections import defaultdict
from statistics import mean
from typing import Any, Dict, Iterable, List

from src.learning.labels import recommendation_for

MIN_EVIDENCE_COUNT = 5
MIN_EXCESS_DAMAGE_PCT = -3.0
PROTECTIVE_LABELS = {"risk_engine_saved_us"}


def _loads(value: Any) -> Dict[str, Any]:
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def build_lessons(reviews: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Build one lesson per label from persisted decision reviews."""
    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for review in reviews:
        label = str(review.get("label") or "insufficient_evidence")
        if label == "insufficient_evidence":
            continue
        groups[label].append(review)

    lessons: List[Dict[str, Any]] = []
    for label, rows in sorted(groups.items()):
        excess_values = [
            float(row["excess_return_pct"])
            for row in rows
            if row.get("excess_return_pct") is not None
        ]
        stock_values = [
            float(row["stock_return_pct"])
            for row in rows
            if row.get("stock_return_pct") is not None
        ]
        symbols = {str(row.get("symbol") or "") for row in rows if row.get("symbol")}
        review_dates = {
            str(row.get("review_date") or "") for row in rows if row.get("review_date")
        }
        avg_excess = mean(excess_values) if excess_values else None
        avg_stock = mean(stock_values) if stock_values else None

        enough_span = len(symbols) >= 2 or len(review_dates) >= 2
        harmful = avg_excess is not None and avg_excess <= MIN_EXCESS_DAMAGE_PCT
        protective = label in PROTECTIVE_LABELS and avg_excess is not None
        actionable = len(rows) >= MIN_EVIDENCE_COUNT and enough_span and (harmful or protective)
        confidence = min(0.95, 0.35 + len(rows) * 0.08)
        if not actionable:
            confidence = min(confidence, 0.55)

        lessons.append(
            {
                "label": label,
                "status": "actionable" if actionable else "watch",
                "evidence_count": len(rows),
                "symbol_count": len(symbols),
                "review_date_count": len(review_dates),
                "avg_excess_return_pct": round(avg_excess, 2) if avg_excess is not None else None,
                "avg_stock_return_pct": round(avg_stock, 2) if avg_stock is not None else None,
                "confidence": round(confidence, 2),
                "recommendation": recommendation_for(label),
                "evidence": {
                    "symbols": sorted(symbols)[:10],
                    "review_windows": sorted(
                        {
                            int(row["review_window_days"])
                            for row in rows
                            if row.get("review_window_days") is not None
                        }
                    ),
                    "sample_reviews": [
                        {
                            "proposal_id": row.get("proposal_id"),
                            "symbol": row.get("symbol"),
                            "excess_return_pct": row.get("excess_return_pct"),
                            "evidence": _loads(row.get("evidence")),
                        }
                        for row in rows[:5]
                    ],
                },
            }
        )

    lessons.sort(key=lambda row: (row["status"] != "actionable", -row["evidence_count"], row["label"]))
    return lessons
