"""Evaluate past proposals and persist advisory learning reviews."""

from __future__ import annotations

import json
import pickle
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from src.config import Config, get_config
from src.journal.repository import JournalRepository
from src.learning.labels import classify_review, recommendation_for
from src.learning.lessons import build_lessons
from src.logging_setup import get_logger

logger = get_logger(__name__)

REVIEW_WINDOWS = (30, 90, 180, 365)


@dataclass
class LearningRunSummary:
    reviews_written: int = 0
    lessons_written: int = 0
    eligible_proposals: int = 0

    def describe(self) -> str:
        return (
            f"learning reviewed {self.eligible_proposals} proposal(s), "
            f"wrote {self.reviews_written} review(s), "
            f"refreshed {self.lessons_written} lesson(s)"
        )


def _parse_date(value: Any) -> Optional[date]:
    if not value:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value)).date()
    except ValueError:
        return None


def _safe_json(value: Any, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


class LearningEvaluator:
    """Review decisions after fixed windows and aggregate lessons."""

    def __init__(self, config: Optional[Config] = None):
        self._config = config or get_config()
        self._repo = JournalRepository(self._config)
        self._frames: Optional[Dict[str, pd.DataFrame]] = None

    def run(self, as_of: Optional[date] = None) -> LearningRunSummary:
        as_of = as_of or date.today()
        proposals = self._repo.reviewable_proposals()
        summary = LearningRunSummary(eligible_proposals=len(proposals))

        for proposal in proposals:
            proposal_date = self._proposal_date(proposal)
            if proposal_date is None:
                continue
            for window in REVIEW_WINDOWS:
                review_date = proposal_date + timedelta(days=window)
                if review_date > as_of:
                    continue
                review = self._build_review(proposal, proposal_date, review_date, window)
                self._repo.upsert_decision_review(review)
                summary.reviews_written += 1

        lessons = build_lessons(self._repo.decision_reviews(limit=10_000))
        self._repo.clear_learning_lessons()
        for lesson in lessons:
            self._repo.upsert_learning_lesson(lesson)
            summary.lessons_written += 1

        logger.info(summary.describe())
        return summary

    def _proposal_date(self, proposal: Dict[str, Any]) -> Optional[date]:
        return (
            _parse_date(proposal.get("trade_executed_at"))
            or _parse_date(proposal.get("decided_at"))
            or _parse_date(proposal.get("created_at"))
            or _parse_date(proposal.get("run_date"))
        )

    def _build_review(
        self,
        proposal: Dict[str, Any],
        proposal_date: date,
        review_date: date,
        window: int,
    ) -> Dict[str, Any]:
        symbol = str(proposal["symbol"]).upper()
        start_price = self._start_price(proposal)
        end_price, stock_price_date = self._price_on_or_before(symbol, review_date)
        stock_return = (
            (end_price / start_price - 1.0) * 100.0
            if start_price and end_price and start_price > 0
            else None
        )

        benchmark_start = self._benchmark_on_or_before(proposal_date)
        benchmark_end = self._benchmark_on_or_before(review_date)
        benchmark_return = (
            (benchmark_end / benchmark_start - 1.0) * 100.0
            if benchmark_start and benchmark_end and benchmark_start > 0
            else None
        )
        excess = (
            stock_return - benchmark_return
            if stock_return is not None and benchmark_return is not None
            else None
        )
        max_drawdown = self._max_drawdown(symbol, proposal_date, review_date, start_price)

        label = classify_review(proposal, stock_return, benchmark_return, excess)
        evidence = {
            "proposal_status": proposal.get("status"),
            "decided_by": proposal.get("decided_by"),
            "source": proposal.get("source"),
            "reference_price": proposal.get("reference_price"),
            "trade_price": proposal.get("trade_price"),
            "start_price": start_price,
            "end_price": end_price,
            "stock_price_date": stock_price_date.isoformat() if stock_price_date else None,
            "benchmark_start": benchmark_start,
            "benchmark_end": benchmark_end,
            "trigger_types": _safe_json(proposal.get("trigger_types"), []),
            "warnings": _safe_json(proposal.get("warnings"), []),
        }

        return {
            "proposal_id": int(proposal["id"]),
            "trade_id": proposal.get("trade_id"),
            "review_window_days": window,
            "proposal_date": proposal_date.isoformat(),
            "review_date": review_date.isoformat(),
            "symbol": symbol,
            "action": proposal["action"],
            "status": proposal["status"],
            "label": label,
            "stock_return_pct": round(stock_return, 2) if stock_return is not None else None,
            "benchmark_return_pct": round(benchmark_return, 2) if benchmark_return is not None else None,
            "excess_return_pct": round(excess, 2) if excess is not None else None,
            "max_drawdown_pct": round(max_drawdown, 2) if max_drawdown is not None else None,
            "realised_pnl": proposal.get("trade_realised_pnl"),
            "evidence": evidence,
            "recommendation": recommendation_for(label),
        }

    @staticmethod
    def _start_price(proposal: Dict[str, Any]) -> Optional[float]:
        value = proposal.get("trade_price") or proposal.get("reference_price")
        try:
            price = float(value)
        except (TypeError, ValueError):
            return None
        return price if price > 0 else None

    def _load_frames(self) -> Dict[str, pd.DataFrame]:
        if self._frames is not None:
            return self._frames
        path = self._config.cache_dir / "price_history.pkl"
        if not path.exists():
            self._frames = {}
            return self._frames
        try:
            with Path(path).open("rb") as handle:
                payload = pickle.load(handle)
        except Exception as exc:
            logger.warning("Could not read price cache for learning: %s", exc)
            self._frames = {}
            return self._frames
        self._frames = payload if isinstance(payload, dict) else {}
        return self._frames

    def _frame_for(self, symbol: str) -> Optional[pd.DataFrame]:
        frames = self._load_frames()
        frame = frames.get(symbol)
        if frame is not None:
            return frame
        return frames.get(f"{symbol}.NS")

    def _price_on_or_before(self, symbol: str, target: date) -> Tuple[Optional[float], Optional[date]]:
        frame = self._frame_for(symbol)
        if frame is None or frame.empty or "Close" not in frame.columns:
            return None, None
        sliced = frame.loc[frame.index <= pd.Timestamp(target)]
        if sliced.empty:
            return None, None
        cleaned = sliced.dropna(subset=["Close"])
        if cleaned.empty:
            return None, None
        ts = cleaned.index[-1]
        price_date = ts.date() if hasattr(ts, "date") else target
        return float(cleaned["Close"].iloc[-1]), price_date

    def _benchmark_on_or_before(self, target: date) -> Optional[float]:
        history = self._repo.nav_history()
        eligible = [
            row for row in history
            if row.get("benchmark_close") is not None
            and str(row.get("run_date")) <= target.isoformat()
        ]
        if not eligible:
            return None
        return float(eligible[-1]["benchmark_close"])

    def _max_drawdown(
        self,
        symbol: str,
        start: date,
        end: date,
        start_price: Optional[float],
    ) -> Optional[float]:
        frame = self._frame_for(symbol)
        if frame is None or frame.empty or "Close" not in frame.columns or not start_price:
            return None
        sliced = frame.loc[
            (frame.index >= pd.Timestamp(start)) & (frame.index <= pd.Timestamp(end))
        ].dropna(subset=["Close"])
        if sliced.empty:
            return None
        peak = start_price
        worst = 0.0
        for close in sliced["Close"].astype(float):
            peak = max(peak, float(close))
            if peak > 0:
                worst = min(worst, (float(close) / peak - 1.0) * 100.0)
        return worst
