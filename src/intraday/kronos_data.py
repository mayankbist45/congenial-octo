"""Build Kronos-ready intraday windows from OHLCV frames."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Dict, Iterable, Optional

import pandas as pd

from src.config import Config

REQUIRED_COLUMNS = ("open", "high", "low", "close")
OPTIONAL_COLUMNS = ("volume", "amount")


@dataclass
class KronosWindow:
    symbol: str
    history: pd.DataFrame
    x_timestamp: pd.Series
    y_timestamp: pd.Series
    pred_len: int
    truncated: bool = False


def build_kronos_window(
    config: Config,
    symbol: str,
    frame: pd.DataFrame,
) -> Optional[KronosWindow]:
    if frame.empty or any(column not in frame.columns for column in REQUIRED_COLUMNS):
        return None

    pred_len = max(int(config.get("intraday.kronos.pred_len", 3) or 3), 1)
    lookback = max(int(config.get("intraday.kronos.lookback_bars", 128) or 128), 8)
    max_context = max(int(config.get("intraday.kronos.max_context", 512) or 512), 8)
    length = min(lookback, max_context)
    if len(frame) < length:
        return None

    history = _prepare_history(frame.tail(length))
    step = _infer_step(history.index)
    if step is None:
        return None

    last_ts = pd.Timestamp(history.index[-1])
    y_index = [last_ts + step * (i + 1) for i in range(pred_len)]
    return KronosWindow(
        symbol=symbol,
        history=history,
        x_timestamp=pd.Series(pd.DatetimeIndex(history.index)),
        y_timestamp=pd.Series(pd.DatetimeIndex(y_index)),
        pred_len=pred_len,
        truncated=len(frame) > length,
    )


def build_kronos_windows(
    config: Config,
    frames: Dict[str, pd.DataFrame],
    symbols: Optional[Iterable[str]] = None,
) -> Dict[str, KronosWindow]:
    selected = list(symbols) if symbols is not None else list(frames.keys())
    windows: Dict[str, KronosWindow] = {}
    for symbol in selected:
        frame = frames.get(symbol)
        if frame is None:
            continue
        window = build_kronos_window(config, symbol, frame)
        if window is not None:
            windows[symbol] = window
    return windows


def _prepare_history(frame: pd.DataFrame) -> pd.DataFrame:
    history = frame.copy()
    history = history.loc[:, [c for c in (*REQUIRED_COLUMNS, *OPTIONAL_COLUMNS) if c in history.columns]]
    if "volume" not in history.columns:
        history["volume"] = 0.0
    history["volume"] = history["volume"].fillna(0.0).astype(float)
    if "amount" not in history.columns:
        history["amount"] = history["close"].astype(float) * history["volume"]
    else:
        history["amount"] = history["amount"].fillna(
            history["close"].astype(float) * history["volume"]
        )
    return history.loc[:, ["open", "high", "low", "close", "volume", "amount"]].astype(float)


def _infer_step(index: pd.Index) -> Optional[timedelta]:
    if not isinstance(index, pd.DatetimeIndex) or len(index) < 2:
        return None
    diffs = index.to_series().diff().dropna()
    if diffs.empty:
        return None
    step = diffs.mode().iloc[0]
    if pd.isna(step) or step <= pd.Timedelta(0):
        return None
    return step.to_pytimedelta()
