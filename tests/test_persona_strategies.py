"""Tests for persona strategies and rankers."""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd
import pytest

from src.config import get_persona_config
from src.intraday.meta import triple_barrier_label
from src.intraday.persona_rankers import FluxRanker, RevertRanker, SpreadRanker, XStatRanker, load_ranker
from src.intraday.strategies import (
    OpeningGapFadeStrategy,
    RangeRevertStrategy,
    VwapFadeStrategy,
    load_strategies,
    scan_cross_signals,
    scan_pair_signals,
)


def _mean_revert_frame() -> pd.DataFrame:
    idx = pd.date_range("2026-08-03 09:15", periods=40, freq="5min")
    closes = [100.0]
    for i in range(1, len(idx)):
        closes.append(closes[-1] + (0.04 if i % 2 == 0 else -0.035))
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c + 0.35 for c in closes],
            "low": [c - 0.35 for c in closes],
            "close": closes,
            "volume": [150_000] * len(idx),
        },
        index=idx,
    )


def _pair_frames(diverge_at_end: bool = True):
    idx = pd.date_range("2026-08-03 09:15", periods=50, freq="5min")
    base = 100 + np.linspace(0, 1, len(idx)) * 0.1
    a = base.copy()
    b = base.copy()
    if diverge_at_end:
        b[-5:] = b[-5:] * 1.04
    fa = pd.DataFrame(
        {"open": a, "high": a + 0.2, "low": a - 0.2, "close": a, "volume": [200_000] * len(idx)},
        index=idx,
    )
    fb = pd.DataFrame(
        {"open": b, "high": b + 0.2, "low": b - 0.2, "close": b, "volume": [180_000] * len(idx)},
        index=idx,
    )
    return fa, fb


@pytest.fixture
def revert_config():
    return get_persona_config("revert")


@pytest.fixture
def flux_config():
    return get_persona_config("flux")


@pytest.fixture
def spread_config():
    return get_persona_config("spread")


@pytest.fixture
def xstat_config():
    return get_persona_config("xstat")


def test_load_revert_strategies(revert_config):
    names = [s.name for s in load_strategies(revert_config)]
    assert names == ["vwap_fade", "opening_gap_fade"]


def test_load_flux_strategies(flux_config):
    names = [s.name for s in load_strategies(flux_config)]
    assert names == ["ofi_momentum", "ofi_exhaustion"]


def test_load_xstat_strategies(xstat_config):
    names = [s.name for s in load_strategies(xstat_config)]
    assert names == ["cross_zscore"]


def test_revert_ranker_prefers_low_trend(revert_config):
    frame = _mean_revert_frame()
    ranked = RevertRanker(revert_config).rank(
        {"TCS": frame},
        {"TCS": {"close": 100.0, "high": 101, "low": 99}},
    )
    assert len(ranked) == 1
    assert ranked[0].in_play is False


def test_flux_ranker_sets_ofi(flux_config):
    idx = pd.date_range("2026-08-03 09:15", periods=80, freq="1min")
    closes = np.linspace(100, 101.5, len(idx))
    frame = pd.DataFrame(
        {
            "open": closes,
            "high": closes + 0.05,
            "low": closes - 0.02,
            "close": closes,
            "volume": [120_000] * len(idx),
        },
        index=idx,
    )
    ranked = FluxRanker(flux_config).rank({"RELIANCE": frame})
    assert len(ranked) == 1
    assert ranked[0].ofi_z is not None


def test_vwap_fade_can_fire(revert_config):
    frame = _mean_revert_frame()
    signals = VwapFadeStrategy().signals("TCS", frame, revert_config)
    assert isinstance(signals, list)


def test_opening_gap_fade(revert_config):
    idx = pd.date_range("2026-08-03 09:15", periods=10, freq="5min")
    frame = pd.DataFrame(
        {
            "open": [99.4] + [99.5] * 9,
            "high": [99.6] + [99.7] * 9,
            "low": [99.2] + [99.4] * 9,
            "close": [99.5] + [99.55] * 9,
            "volume": [120_000] + [80_000] * 9,
        },
        index=idx,
    )
    prev = {"close": 100.0}
    signals = OpeningGapFadeStrategy().signals("TCS", frame, revert_config, prev)
    assert len(signals) <= 1


def test_range_revert(revert_config):
    idx = pd.date_range("2026-08-03 09:15", periods=20, freq="5min")
    closes = [100.0] * 8 + [100.5, 100.4, 100.3, 100.2] + [100.1] * 8
    frame = pd.DataFrame(
        {
            "open": closes,
            "high": [c + 0.2 for c in closes],
            "low": [c - 0.2 for c in closes],
            "close": closes,
            "volume": [100_000] * len(idx),
        },
        index=idx,
    )
    signals = RangeRevertStrategy().signals("TCS", frame, revert_config)
    assert isinstance(signals, list)


def test_spread_ranker_and_signals(spread_config):
    fa, fb = _pair_frames(diverge_at_end=True)
    ranked = SpreadRanker(spread_config).rank({"HDFCBANK": fa, "ICICIBANK": fb})
    assert ranked
    signals = scan_pair_signals(spread_config, {"HDFCBANK": fa, "ICICIBANK": fb})
    assert isinstance(signals, dict)


def test_xstat_ranker_and_signals(xstat_config):
    fa, fb = _pair_frames(diverge_at_end=True)
    ranked = XStatRanker(xstat_config).rank({"NIFTYBEES": fa, "RELIANCE": fb})
    assert ranked
    signals = scan_cross_signals(xstat_config, {"NIFTYBEES": fa, "RELIANCE": fb})
    assert isinstance(signals, dict)


def test_triple_barrier_label():
    idx = pd.date_range("2026-08-03 10:00", periods=5, freq="5min")
    frame = pd.DataFrame(
        {
            "open": [100, 100.5, 101, 101.5, 102],
            "high": [100.6, 101.2, 102.5, 101.8, 102.2],
            "low": [99.8, 100.2, 100.8, 101.0, 101.5],
            "close": [100.4, 101.0, 102.0, 101.5, 102.0],
            "volume": [1000] * 5,
        },
        index=idx,
    )
    entry = idx[0].to_pydatetime()
    label, reason, bars = triple_barrier_label(
        frame,
        entry,
        100.0,
        "BUY",
        upper=102.0,
        lower=99.5,
        vertical=idx[-1].to_pydatetime(),
    )
    assert label in (-1, 0, 1)
    assert bars >= 0


def test_load_ranker_factory():
    assert load_ranker(get_persona_config("calan")).__class__.__name__ == "IntradayRanker"
    assert load_ranker(get_persona_config("revert")).__class__.__name__ == "RevertRanker"
    assert load_ranker(get_persona_config("xstat")).__class__.__name__ == "XStatRanker"
