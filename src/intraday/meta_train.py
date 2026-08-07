"""Build labeled training rows for the meta-label model."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any, Dict, List, Optional

import pandas as pd

from src.config import Config
from src.intraday.engine_helpers import collect_signals
from src.intraday.meta import meta_label_from_barrier, triple_barrier_label
from src.intraday.meta_log import build_gate_features
from src.intraday.persona_rankers import filter_ranked_for_persona, load_ranker
from src.journal.repository import JournalRepository
from src.logging_setup import get_logger

logger = get_logger(__name__)


def rows_from_journal(config: Config, *, limit: int = 5000) -> List[Dict[str, Any]]:
    """Labeled meta_decisions from Postgres/SQLite."""
    repo = JournalRepository(config)
    out: List[Dict[str, Any]] = []
    for row in repo.meta_decisions(limit=limit):
        if row.get("meta_label") not in (0, 1):
            continue
        out.append(
            {
                "features": row.get("features") or {},
                "meta_label": int(row["meta_label"]),
                "source": "journal",
                "symbol": row.get("symbol"),
                "decision_ts": row.get("decision_ts"),
            }
        )
    return out


def rows_from_frames(
    config: Config,
    frames: Dict[str, pd.DataFrame],
    previous_days: Optional[Dict[str, Dict[str, float]]] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
) -> List[Dict[str, Any]]:
    """Label every rule signal with triple-barrier outcomes (bypass live gate)."""
    previous_days = previous_days or {}
    ranker = load_ranker(config)
    days = sorted({ts.date() for frame in frames.values() for ts in frame.index})
    if start_date:
        days = [d for d in days if d >= start_date]
    if end_date:
        days = [d for d in days if d <= end_date]

    rows: List[Dict[str, Any]] = []
    for day in days:
        day_frames: Dict[str, pd.DataFrame] = {}
        day_previous: Dict[str, Dict[str, float]] = {}
        for symbol, frame in frames.items():
            day_frame = frame.loc[frame.index.date == day]
            if not day_frame.empty:
                day_frames[symbol] = day_frame
            prior = frame.loc[frame.index.date < day]
            if not prior.empty:
                day_previous[symbol] = {
                    "close": float(prior["close"].iloc[-1]),
                    "high": float(prior["high"].max()),
                    "low": float(prior["low"].min()),
                }
            elif symbol in previous_days:
                day_previous[symbol] = previous_days[symbol]
        if not day_frames:
            continue

        ranked = ranker.rank(day_frames, day_previous)
        limit = int(config.get("intraday.ranked_universe_size", 8) or 8)
        ranked = ranked[:limit]
        pool = filter_ranked_for_persona(config, ranked) or ranked
        collected = collect_signals(config, day_frames, pool, day_previous)

        force_exit = _force_exit(config, day)
        for _, signal, item in collected:
            frame = day_frames.get(signal.symbol)
            if frame is None or frame.empty:
                continue
            vertical = signal.max_exit_time or force_exit
            barrier, exit_reason, _bars = triple_barrier_label(
                frame,
                signal.entry_time,
                signal.entry_price,
                signal.side,
                signal.target_price,
                signal.stop_price,
                vertical,
            )
            meta_y = meta_label_from_barrier(barrier)
            features = build_gate_features(config, signal, item, estimated_costs=0.0)
            rows.append(
                {
                    "features": features,
                    "meta_label": meta_y,
                    "outcome_label": barrier,
                    "exit_reason": exit_reason,
                    "source": "backtest",
                    "symbol": signal.symbol,
                    "decision_ts": signal.entry_time.isoformat(timespec="seconds"),
                }
            )
    logger.info(
        "Built %d meta training rows from frames (%d days)", len(rows), len(days)
    )
    return rows


def _force_exit(config: Config, day: date) -> datetime:
    raw = str(config.get("intraday.force_exit_time", "15:15") or "15:15")
    hour, minute = [int(x) for x in raw.split(":")[:2]]
    return datetime.combine(day, time(hour, minute))


def merge_training_rows(*groups: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    merged: List[Dict[str, Any]] = []
    for group in groups:
        merged.extend(group)
    return merged
