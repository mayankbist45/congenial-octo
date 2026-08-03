"""Candle-based order flow imbalance (OFI) proxy helpers."""

from __future__ import annotations

import pandas as pd


def signed_volume(frame: pd.DataFrame) -> pd.Series:
    rng = (frame["high"] - frame["low"]).replace(0, pd.NA)
    clv = 2 * (frame["close"] - frame["low"]) / rng - 1
    return frame["volume"] * clv.fillna(0)


def ofi_z_proxy(frame: pd.DataFrame, lookback: int = 5, z_lookback: int = 60) -> float:
    signed = signed_volume(frame)
    if len(signed) < lookback + 2:
        return 0.0
    ofi_n = signed.rolling(lookback).sum()
    window = ofi_n.tail(min(z_lookback, len(ofi_n)))
    if len(window) < lookback + 2:
        return 0.0
    mu = float(window.mean())
    sigma = float(window.std())
    if sigma <= 1e-9:
        return 0.0
    return float((ofi_n.iloc[-1] - mu) / sigma)


def ofi_divergence(frame: pd.DataFrame) -> bool:
    if len(frame) < 3:
        return False
    ofi_z = ofi_z_proxy(frame)
    price_delta = float(frame["close"].iloc[-1] - frame["close"].iloc[-2])
    if ofi_z == 0 or price_delta == 0:
        return False
    return (ofi_z > 0) != (price_delta > 0)
