"""Offline Kronos evaluation helpers for intraday windows."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Dict, Iterable, List

import pandas as pd

from src.config import Config
from src.intraday.kronos_service import KronosForecastResult, get_kronos_service


@dataclass
class KronosEvalRow:
    symbol: str
    market_id: str
    as_of: str
    pred_len: int
    current_close: float
    realized_close: float
    realized_return_pct: float
    realized_max_up_pct: float
    realized_max_down_pct: float
    directional_hit: bool
    trend_bias: str
    quality_score: float
    expected_return_pct: float
    confidence: float
    rationale: str
    source: str

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


def build_kronos_eval_rows(
    config: Config,
    frames: Dict[str, pd.DataFrame],
    *,
    stride: int = 3,
) -> List[KronosEvalRow]:
    """Evaluate Kronos against realized forward returns on intraday frames."""
    service = get_kronos_service(config)
    rows: List[KronosEvalRow] = []
    pred_len = max(int(config.get("intraday.kronos.pred_len", 3) or 3), 1)
    lookback = max(int(config.get("intraday.kronos.lookback_bars", 128) or 128), 8)
    min_bars = max(lookback, 8)

    for symbol, frame in frames.items():
        if frame.empty or "close" not in frame.columns:
            continue
        for day_frame in _iter_session_frames(frame):
            day_frame = day_frame.sort_index()
            if len(day_frame) < min_bars + pred_len:
                continue
            for idx in range(min_bars - 1, len(day_frame) - pred_len, max(stride, 1)):
                history = day_frame.iloc[: idx + 1].copy()
                result = service.score_frames({symbol: history}).get(symbol)
                if result is None or not result.available:
                    continue
                current_close = float(day_frame["close"].iloc[idx])
                realized = day_frame.iloc[idx + 1 : idx + 1 + pred_len]
                realized_close = float(realized["close"].iloc[-1])
                realized_return_pct = _pct_change(current_close, realized_close)
                realized_max_up_pct = _pct_change(current_close, float(realized["high"].max()))
                realized_max_down_pct = _pct_change(current_close, float(realized["low"].min()))
                rows.append(
                    KronosEvalRow(
                        symbol=symbol,
                        market_id=config.market_id,
                        as_of=pd.Timestamp(day_frame.index[idx]).isoformat(),
                        pred_len=pred_len,
                        current_close=current_close,
                        realized_close=realized_close,
                        realized_return_pct=realized_return_pct,
                        realized_max_up_pct=realized_max_up_pct,
                        realized_max_down_pct=realized_max_down_pct,
                        directional_hit=_directional_hit(result, realized_return_pct),
                        trend_bias=str(result.trend_bias or "neutral"),
                        quality_score=float(result.quality_score or 0.0),
                        expected_return_pct=float(result.expected_return_pct or 0.0),
                        confidence=float(result.confidence or 0.0),
                        rationale=str(result.rationale or ""),
                        source=str(result.source or "kronos"),
                    )
                )
    return rows


def summarize_kronos_eval(rows: Iterable[KronosEvalRow]) -> Dict[str, object]:
    data = [row.to_dict() if isinstance(row, KronosEvalRow) else dict(row) for row in rows]
    if not data:
        return {
            "rows": 0,
            "directional_hit_rate": None,
            "expected_realized_corr": None,
            "avg_expected_return_pct": None,
            "avg_realized_return_pct": None,
        }
    frame = pd.DataFrame(data)
    hit_rate = float(frame["directional_hit"].mean()) if "directional_hit" in frame else None
    corr = None
    if frame["expected_return_pct"].nunique() > 1 and frame["realized_return_pct"].nunique() > 1:
        corr = float(frame["expected_return_pct"].corr(frame["realized_return_pct"]))
    return {
        "rows": int(len(frame)),
        "directional_hit_rate": round(hit_rate, 4) if hit_rate is not None else None,
        "expected_realized_corr": round(corr, 4) if corr is not None else None,
        "avg_expected_return_pct": round(float(frame["expected_return_pct"].mean()), 4),
        "avg_realized_return_pct": round(float(frame["realized_return_pct"].mean()), 4),
        "avg_confidence": round(float(frame["confidence"].mean()), 4),
        "markets": sorted(frame["market_id"].dropna().astype(str).unique().tolist()),
        "symbols": int(frame["symbol"].nunique()),
    }


def _iter_session_frames(frame: pd.DataFrame):
    if not isinstance(frame.index, pd.DatetimeIndex):
        yield frame
        return
    for _, group in frame.groupby(frame.index.date):
        yield group


def _pct_change(start: float, end: float) -> float:
    if start == 0:
        return 0.0
    return (end / start - 1.0) * 100.0


def _directional_hit(result: KronosForecastResult, realized_return_pct: float) -> bool:
    bias = str(result.trend_bias or "neutral").lower()
    if bias == "bullish":
        return realized_return_pct >= 0
    if bias == "bearish":
        return realized_return_pct <= 0
    return abs(realized_return_pct) < 0.1
