"""Read-side service for dashboard learning views."""

from __future__ import annotations

import json
from collections import Counter
from typing import Any, Dict, List, Optional

from src.config import Config, get_config
from src.journal.repository import JournalRepository


def _loads(value: Any, default: Any) -> Any:
    if not value:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


class LearningService:
    """Dashboard-friendly read model for learning reviews and lessons."""

    def __init__(self, config: Optional[Config] = None):
        self._config = config or get_config()
        self._repo = JournalRepository(self._config)

    def summary(self) -> Dict[str, Any]:
        reviews = self.reviews(limit=10_000)
        lessons = self.lessons()
        labels = Counter(r["label"] for r in reviews)
        latest_review = max((r["review_date"] for r in reviews), default=None)
        actionable = [l for l in lessons if l["status"] == "actionable"]
        return {
            "reviewed_decisions": len(reviews),
            "actionable_lessons": len(actionable),
            "watch_lessons": len(lessons) - len(actionable),
            "latest_review_date": latest_review,
            "common_labels": [
                {"label": label, "count": count}
                for label, count in labels.most_common(5)
            ],
        }

    def lessons(self) -> List[Dict[str, Any]]:
        return [
            {
                "id": row["id"],
                "label": row["label"],
                "status": row["status"],
                "evidence_count": row["evidence_count"],
                "symbol_count": row["symbol_count"],
                "review_date_count": row["review_date_count"],
                "avg_excess_return_pct": row["avg_excess_return_pct"],
                "avg_stock_return_pct": row["avg_stock_return_pct"],
                "confidence": row["confidence"],
                "recommendation": row["recommendation"] or "",
                "evidence": _loads(row["evidence"], {}),
                "updated_at": row["updated_at"],
            }
            for row in self._repo.learning_lessons()
        ]

    def reviews(self, limit: int = 100) -> List[Dict[str, Any]]:
        return [
            {
                "id": row["id"],
                "proposal_id": row["proposal_id"],
                "trade_id": row["trade_id"],
                "review_window_days": row["review_window_days"],
                "proposal_date": row["proposal_date"],
                "review_date": row["review_date"],
                "symbol": row["symbol"],
                "action": row["action"],
                "status": row["status"],
                "label": row["label"],
                "stock_return_pct": row["stock_return_pct"],
                "benchmark_return_pct": row["benchmark_return_pct"],
                "excess_return_pct": row["excess_return_pct"],
                "max_drawdown_pct": row["max_drawdown_pct"],
                "realised_pnl": row["realised_pnl"],
                "evidence": _loads(row["evidence"], {}),
                "recommendation": row["recommendation"] or "",
                "updated_at": row["updated_at"],
            }
            for row in self._repo.decision_reviews(limit=limit)
        ]

    def payload(self) -> Dict[str, Any]:
        return {
            "summary": self.summary(),
            "lessons": self.lessons(),
            "reviews": self.reviews(limit=100),
        }
