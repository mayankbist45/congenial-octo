"""Market regime classification from price-derived risk signals."""

from __future__ import annotations

import math
from datetime import date
from typing import Dict, List, Optional, Tuple

import pandas as pd

from src.market_data.indicators import sma
from src.models import MarketRegimeSnapshot, TechnicalSnapshot
from src.risk_analytics.factor_model import daily_returns

TRADING_DAYS = 252
VOL_WINDOW = 21
LOOKBACK = 252
CORRELATION_WINDOW = 60


def _clean_prices(series: Optional[pd.Series]) -> pd.Series:
    if series is None:
        return pd.Series(dtype=float)
    return series.astype(float).replace([math.inf, -math.inf], pd.NA).dropna()


def _as_of(series: pd.Series) -> Optional[date]:
    if series.empty:
        return None
    value = series.index[-1]
    return value.date() if hasattr(value, "date") else None


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return min(max(value, low), high)


def _percentile_rank(values: pd.Series, latest: float) -> float:
    clean = values.dropna()
    if clean.empty:
        return 0.0
    return float((clean <= latest).mean() * 100.0)


def _benchmark_volatility_pressure(
    benchmark_prices: pd.Series,
) -> Optional[Tuple[float, str]]:
    returns = daily_returns(benchmark_prices)
    rolling_vol = (
        returns.rolling(VOL_WINDOW)
        .std()
        .dropna()
        .tail(LOOKBACK)
        * math.sqrt(TRADING_DAYS)
        * 100.0
    )
    if len(rolling_vol) < 60:
        return None

    latest = float(rolling_vol.iloc[-1])
    pressure = _percentile_rank(rolling_vol, latest)
    return (
        round(pressure, 1),
        f"21-day benchmark volatility is {latest:.1f}% annualised "
        f"({pressure:.0f}th percentile of the last year)",
    )


def _benchmark_drawdown_pressure(
    benchmark_prices: pd.Series,
) -> Optional[Tuple[float, str]]:
    window = benchmark_prices.tail(LOOKBACK)
    if len(window) < 60:
        return None
    latest = float(window.iloc[-1])
    high = float(window.max())
    if high <= 0:
        return None

    drawdown_pct = (latest / high - 1.0) * 100.0
    pressure = _clamp(abs(min(drawdown_pct, 0.0)) / 25.0 * 100.0)
    return (
        round(pressure, 1),
        f"benchmark is {drawdown_pct:.1f}% from its trailing one-year high",
    )


def _series_above_200dma(series: pd.Series) -> Optional[bool]:
    clean = _clean_prices(series)
    if len(clean) < 120:
        return None
    average = sma(clean, 200).dropna()
    if average.empty:
        return None
    return bool(float(clean.iloc[-1]) > float(average.iloc[-1]))


def _breadth_pressure(
    price_series: Dict[str, pd.Series],
    technicals: Optional[Dict[str, TechnicalSnapshot]],
) -> Optional[Tuple[float, str]]:
    total = 0
    above = 0
    for symbol, series in price_series.items():
        value: Optional[bool] = None
        technical = technicals.get(symbol) if technicals else None
        if technical and technical.above_sma_200 is not None:
            value = technical.above_sma_200
        else:
            value = _series_above_200dma(series)

        if value is None:
            continue
        total += 1
        if value:
            above += 1

    if total < 5:
        return None

    above_pct = above / total * 100.0
    pressure = 100.0 - above_pct
    return (
        round(pressure, 1),
        f"{above_pct:.0f}% of priced universe names are above their 200-DMA",
    )


def _correlation_pressure(
    price_series: Dict[str, pd.Series],
) -> Optional[Tuple[float, str]]:
    returns = pd.DataFrame(
        {
            symbol: daily_returns(series).tail(CORRELATION_WINDOW)
            for symbol, series in price_series.items()
            if series is not None and len(series) >= CORRELATION_WINDOW + 1
        }
    ).dropna(axis=1, thresh=30)

    if returns.shape[1] < 5:
        return None

    matrix = returns.corr().abs()
    values: List[float] = []
    columns = list(matrix.columns)
    for i, left in enumerate(columns):
        for right in columns[i + 1 :]:
            value = matrix.loc[left, right]
            if pd.notna(value):
                values.append(float(value))

    if not values:
        return None

    average = sum(values) / len(values)
    pressure = _clamp((average - 0.20) / 0.60 * 100.0)
    return (
        round(pressure, 1),
        f"average 60-day pairwise correlation across priced names is {average:.2f}",
    )


def assess_market_regime(
    benchmark_prices: Optional[pd.Series],
    price_series: Dict[str, pd.Series],
    technicals: Optional[Dict[str, TechnicalSnapshot]] = None,
) -> MarketRegimeSnapshot:
    """Classify broad-market stress from simple, auditable price features."""
    benchmark = _clean_prices(benchmark_prices)
    snapshot = MarketRegimeSnapshot(as_of=_as_of(benchmark))

    components: Dict[str, float] = {}
    evidence: List[str] = []

    if not benchmark.empty:
        for name, result in (
            ("volatility", _benchmark_volatility_pressure(benchmark)),
            ("drawdown", _benchmark_drawdown_pressure(benchmark)),
        ):
            if result is not None:
                score, detail = result
                components[name] = score
                evidence.append(detail)

    for name, result in (
        ("breadth", _breadth_pressure(price_series, technicals)),
        ("correlation", _correlation_pressure(price_series)),
    ):
        if result is not None:
            score, detail = result
            components[name] = score
            evidence.append(detail)

    snapshot.components = components
    snapshot.evidence = evidence

    if len(components) < 3:
        snapshot.regime = "unknown"
        snapshot.warnings.append(
            "Market regime unavailable: fewer than three inputs had enough history"
        )
        return snapshot

    weights = {
        "volatility": 0.30,
        "drawdown": 0.25,
        "breadth": 0.25,
        "correlation": 0.20,
    }
    available_weight = sum(weights[name] for name in components)
    score = sum(components[name] * weights[name] for name in components)
    score = score / available_weight if available_weight > 0 else 0.0
    snapshot.score = round(score, 1)

    if score >= 65.0:
        snapshot.regime = "stressed"
        snapshot.warnings.append(
            "Market regime is stressed; discretionary risk should be scrutinized"
        )
    elif score >= 40.0:
        snapshot.regime = "elevated"
        snapshot.warnings.append(
            "Market regime is elevated; new risk should need stronger evidence"
        )
    else:
        snapshot.regime = "calm"

    return snapshot
