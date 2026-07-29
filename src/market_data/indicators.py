"""Technical indicators computed from a daily OHLCV frame.

Implemented directly on pandas rather than pulling in a TA library: the set of
indicators a long-horizon fund needs is small, and having the formulas visible
makes the trend and momentum factor definitions auditable.
"""

from __future__ import annotations

import math
from typing import Optional

import numpy as np
import pandas as pd

TRADING_DAYS_PER_YEAR = 252
TRADING_DAYS_PER_MONTH = 21


def _last_valid(series: pd.Series) -> Optional[float]:
    if series is None or series.empty:
        return None
    cleaned = series.dropna()
    if cleaned.empty:
        return None
    value = float(cleaned.iloc[-1])
    return None if math.isnan(value) or math.isinf(value) else value


def sma(close: pd.Series, window: int) -> pd.Series:
    return close.rolling(window=window, min_periods=max(window // 2, 2)).mean()


def ema(close: pd.Series, span: int) -> pd.Series:
    return close.ewm(span=span, adjust=False, min_periods=max(span // 2, 2)).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI."""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)

    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()

    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    result = 100.0 - (100.0 / (1.0 + rs))
    # An unbroken run of gains has no average loss, which is RSI 100 by
    # definition; the divide above yields NaN there, so set it explicitly.
    no_losses = (avg_loss == 0.0) & avg_gain.notna()
    return result.mask(no_losses, 100.0)


def macd(
    close: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> pd.DataFrame:
    macd_line = ema(close, fast) - ema(close, slow)
    signal_line = macd_line.ewm(
        span=signal, adjust=False, min_periods=max(signal // 2, 2)
    ).mean()
    return pd.DataFrame(
        {
            "macd": macd_line,
            "signal": signal_line,
            "histogram": macd_line - signal_line,
        }
    )


def atr(
    high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14
) -> pd.Series:
    """Average True Range (Wilder smoothing)."""
    previous_close = close.shift(1)
    true_range = pd.concat(
        [
            high - low,
            (high - previous_close).abs(),
            (low - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return true_range.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def trailing_return_pct(close: pd.Series, days: int) -> Optional[float]:
    cleaned = close.dropna()
    if len(cleaned) <= days:
        return None
    start = float(cleaned.iloc[-(days + 1)])
    end = float(cleaned.iloc[-1])
    if start <= 0:
        return None
    return (end / start - 1.0) * 100.0


def annualised_volatility_pct(close: pd.Series, window: int = 252) -> Optional[float]:
    returns = close.pct_change().dropna().tail(window)
    if len(returns) < 20:
        return None
    return float(returns.std() * math.sqrt(TRADING_DAYS_PER_YEAR) * 100.0)


def average_daily_turnover(
    close: pd.Series, volume: pd.Series, window: int = 30
) -> Optional[float]:
    """Rupee turnover per day - the practical measure of tradability."""
    frame = pd.concat([close, volume], axis=1).dropna().tail(window)
    if frame.empty:
        return None
    turnover = frame.iloc[:, 0] * frame.iloc[:, 1]
    value = float(turnover.mean())
    return None if math.isnan(value) else value


def compute_snapshot(frame: pd.DataFrame, symbol: str) -> dict:
    """Compute every indicator the fund uses from one OHLCV frame.

    Returns a plain dict so the caller can build a ``TechnicalSnapshot``
    without this module importing the domain models.
    """
    required = {"Open", "High", "Low", "Close", "Volume"}
    if frame is None or frame.empty or not required.issubset(frame.columns):
        return {"symbol": symbol}

    frame = frame.dropna(subset=["Close"]).copy()
    if frame.empty:
        return {"symbol": symbol}

    close = frame["Close"].astype(float)
    high = frame["High"].astype(float)
    low = frame["Low"].astype(float)
    volume = frame["Volume"].astype(float)

    sma_50 = _last_valid(sma(close, 50))
    sma_200 = _last_valid(sma(close, 200))
    last_price = _last_valid(close)

    macd_frame = macd(close)
    atr_series = atr(high, low, close)
    atr_value = _last_valid(atr_series)

    window_52w = close.tail(TRADING_DAYS_PER_YEAR)
    high_52w = float(window_52w.max()) if not window_52w.empty else None
    low_52w = float(window_52w.min()) if not window_52w.empty else None

    snapshot = {
        "symbol": symbol,
        "as_of": frame.index[-1].date() if hasattr(frame.index[-1], "date") else None,
        "last_price": last_price,
        "sma_50": sma_50,
        "sma_200": sma_200,
        "rsi_14": _last_valid(rsi(close)),
        "macd": _last_valid(macd_frame["macd"]),
        "macd_signal": _last_valid(macd_frame["signal"]),
        "macd_histogram": _last_valid(macd_frame["histogram"]),
        "atr_14": atr_value,
        "atr_pct": (
            (atr_value / last_price * 100.0)
            if atr_value and last_price and last_price > 0
            else None
        ),
        "return_1m": trailing_return_pct(close, TRADING_DAYS_PER_MONTH),
        "return_3m": trailing_return_pct(close, TRADING_DAYS_PER_MONTH * 3),
        "return_6m": trailing_return_pct(close, TRADING_DAYS_PER_MONTH * 6),
        "return_12m": trailing_return_pct(close, TRADING_DAYS_PER_YEAR),
        "volatility_annualised": annualised_volatility_pct(close),
        "avg_daily_turnover": average_daily_turnover(close, volume),
        "distance_from_52w_high": (
            (last_price / high_52w - 1.0) * 100.0
            if last_price and high_52w and high_52w > 0
            else None
        ),
        "distance_from_52w_low": (
            (last_price / low_52w - 1.0) * 100.0
            if last_price and low_52w and low_52w > 0
            else None
        ),
        "above_sma_200": (
            bool(last_price > sma_200) if last_price and sma_200 else None
        ),
        "golden_cross": (
            bool(sma_50 > sma_200) if sma_50 and sma_200 else None
        ),
    }
    return snapshot
