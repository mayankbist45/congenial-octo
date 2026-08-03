"""Lopez de Prado meta-labeling helpers (triple-barrier labels)."""

from __future__ import annotations

from datetime import datetime
from typing import Optional, Tuple

import pandas as pd


def triple_barrier_label(
    frame: pd.DataFrame,
    entry_time: datetime,
    entry_price: float,
    side: str,
    upper: float,
    lower: float,
    vertical: datetime,
) -> Tuple[int, str, int]:
    """
    Label a signal outcome: +1 upper, -1 lower, 0 timeout.
    Conservative same-bar rule: stop wins if both touched.
    """
    future = frame.loc[frame.index > pd.Timestamp(entry_time)]
    future = future.loc[future.index <= pd.Timestamp(vertical)]
    if future.empty:
        return 0, "timeout", 0

    for i, (ts, row) in enumerate(future.iterrows()):
        high = float(row["high"])
        low = float(row["low"])
        if side == "BUY":
            hit_upper = high >= upper
            hit_lower = low <= lower
        else:
            hit_upper = low <= upper
            hit_lower = high >= lower
        if hit_upper and hit_lower:
            return -1, "lower_barrier", i + 1
        if hit_upper:
            return 1, "upper_barrier", i + 1
        if hit_lower:
            return -1, "lower_barrier", i + 1
    return 0, "vertical_barrier", len(future)


def meta_label_from_barrier(label: int, min_return: float = 0.0) -> int:
    """Binary meta label: 1 take, 0 skip."""
    return 1 if label == 1 else 0
