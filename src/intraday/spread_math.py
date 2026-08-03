"""Intraday pair spread helpers."""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from src.config import Config


def configured_pairs(config: Config) -> List[Tuple[str, str]]:
    raw = config.get("intraday.pairs") or []
    pairs: List[Tuple[str, str]] = []
    for item in raw:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            pairs.append((str(item[0]).upper(), str(item[1]).upper()))
    return pairs


def spread_series(fa: pd.DataFrame, fb: pd.DataFrame) -> pd.Series:
    n = min(len(fa), len(fb))
    if n < 2:
        return pd.Series(dtype=float)
    a = fa["close"].tail(n).astype(float).values
    b = fb["close"].tail(n).astype(float).values
    with np.errstate(divide="ignore", invalid="ignore"):
        spread = np.log(a / b)
    idx = fa.index[-n:]
    return pd.Series(spread[np.isfinite(spread)], index=idx[-len(spread[np.isfinite(spread)]) :])


def spread_z_at(
    fa: pd.DataFrame,
    fb: pd.DataFrame,
    min_bars: int = 30,
) -> Optional[float]:
    spread = spread_series(fa, fb)
    if len(spread) < min_bars:
        return None
    mu = float(spread.mean())
    sigma = float(spread.std())
    if sigma <= 1e-9:
        return None
    return float((spread.iloc[-1] - mu) / sigma)


def spread_z_history(
    fa: pd.DataFrame,
    fb: pd.DataFrame,
    min_bars: int = 30,
) -> pd.Series:
    n = min(len(fa), len(fb))
    if n < min_bars:
        return pd.Series(dtype=float)
    a = fa["close"].tail(n).astype(float).values
    b = fb["close"].tail(n).astype(float).values
    with np.errstate(divide="ignore", invalid="ignore"):
        raw = np.log(a / b)
    idx = fa.index[-n:]
    spread = pd.Series(raw, index=idx)
    spread = spread.replace([np.inf, -np.inf], np.nan).dropna()
    if len(spread) < min_bars:
        return pd.Series(dtype=float)
    mu = spread.expanding(min_periods=min_bars).mean()
    sigma = spread.expanding(min_periods=min_bars).std().replace(0, np.nan)
    return ((spread - mu) / sigma).dropna()


def pair_beta(fa: pd.DataFrame, fb: pd.DataFrame) -> float:
    n = min(len(fa), len(fb))
    if n < 10:
        return 1.0
    a = np.log(fa["close"].tail(n).astype(float).values)
    b = np.log(fb["close"].tail(n).astype(float).values)
    if len(a) != len(b):
        m = min(len(a), len(b))
        a, b = a[-m:], b[-m:]
    var_b = float(np.var(b))
    if var_b <= 1e-12:
        return 1.0
    cov = float(np.cov(a, b)[0, 1])
    return cov / var_b
