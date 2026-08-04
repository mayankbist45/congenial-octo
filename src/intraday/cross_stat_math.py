"""Cross-asset statistical arbitrage helpers (beta-hedged residual spreads)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

import numpy as np
import pandas as pd

from src.config import Config


@dataclass(frozen=True)
class CrossBasket:
    anchor: str
    leg: str
    beta: Optional[float] = None

    @property
    def basket_id(self) -> str:
        return f"{self.anchor}:{self.leg}"


def configured_cross_baskets(config: Config) -> List[CrossBasket]:
    raw = config.get("intraday.cross_baskets") or []
    baskets: List[CrossBasket] = []
    for item in raw:
        if isinstance(item, dict):
            anchor = str(item.get("anchor", "")).upper()
            leg = str(item.get("leg", "")).upper()
            if not anchor or not leg:
                continue
            beta_raw = item.get("beta")
            beta = float(beta_raw) if beta_raw is not None else None
            baskets.append(CrossBasket(anchor=anchor, leg=leg, beta=beta))
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            baskets.append(CrossBasket(anchor=str(item[0]).upper(), leg=str(item[1]).upper()))
    return baskets


def is_two_leg_persona(config: Config) -> bool:
    return config.persona_id in ("spread", "xstat")


def hedge_beta(
    anchor: pd.DataFrame,
    leg: pd.DataFrame,
    lookback: int = 60,
) -> float:
    n = min(len(anchor), len(leg), lookback)
    if n < 10:
        return 1.0
    la = np.log(anchor["close"].tail(n).astype(float).values)
    ll = np.log(leg["close"].tail(n).astype(float).values)
    var_a = float(np.var(la))
    if var_a <= 1e-12:
        return 1.0
    return float(np.cov(ll, la)[0, 1] / var_a)


def residual_series(
    anchor: pd.DataFrame,
    leg: pd.DataFrame,
    beta: Optional[float] = None,
    beta_lookback: int = 60,
) -> pd.Series:
    n = min(len(anchor), len(leg))
    if n < 2:
        return pd.Series(dtype=float)
    fa = anchor.tail(n)
    fb = leg.tail(n)
    b = beta if beta is not None else hedge_beta(fa, fb, beta_lookback)
    la = np.log(fa["close"].astype(float).values)
    ll = np.log(fb["close"].astype(float).values)
    residual = ll - b * la
    idx = fb.index
    out = pd.Series(residual[np.isfinite(residual)], index=idx[-len(residual[np.isfinite(residual)]) :])
    return out


def residual_z_at(
    anchor: pd.DataFrame,
    leg: pd.DataFrame,
    *,
    beta: Optional[float] = None,
    beta_lookback: int = 60,
    min_bars: int = 30,
) -> Optional[float]:
    spread = residual_series(anchor, leg, beta=beta, beta_lookback=beta_lookback)
    if len(spread) < min_bars:
        return None
    mu = float(spread.mean())
    sigma = float(spread.std())
    if sigma <= 1e-9:
        return None
    return float((spread.iloc[-1] - mu) / sigma)


def residual_z_history(
    anchor: pd.DataFrame,
    leg: pd.DataFrame,
    *,
    beta: Optional[float] = None,
    beta_lookback: int = 60,
    min_bars: int = 30,
) -> pd.Series:
    n = min(len(anchor), len(leg))
    if n < min_bars:
        return pd.Series(dtype=float)
    fa = anchor.tail(n)
    fb = leg.tail(n)
    b = beta if beta is not None else hedge_beta(fa, fb, beta_lookback)
    la = np.log(fa["close"].astype(float).values)
    ll = np.log(fb["close"].astype(float).values)
    idx = fb.index
    raw = ll - b * la
    spread = pd.Series(raw, index=idx)
    spread = spread.replace([np.inf, -np.inf], np.nan).dropna()
    if len(spread) < min_bars:
        return pd.Series(dtype=float)
    mu = spread.expanding(min_periods=min_bars).mean()
    sigma = spread.expanding(min_periods=min_bars).std().replace(0, np.nan)
    return ((spread - mu) / sigma).dropna()
