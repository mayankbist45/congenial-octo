from __future__ import annotations

import pandas as pd

from src.market_data.prices import PriceService


def _frame() -> pd.DataFrame:
    index = pd.date_range("2026-08-01", periods=3, freq="D")
    return pd.DataFrame(
        {
            "Open": [100.0, 101.0, 102.0],
            "High": [101.0, 102.0, 103.0],
            "Low": [99.0, 100.0, 101.0],
            "Close": [100.5, 101.5, 102.5],
            "Volume": [1000, 1100, 1200],
        },
        index=index,
    )


def test_load_history_retries_symbols_missing_from_a_partial_batch(temp_config) -> None:
    service = PriceService(temp_config)
    calls: list[list[str]] = []
    good = _frame()

    def fake_download(symbols: list[str], days: int):
        calls.append(list(symbols))
        if len(symbols) > 1:
            return {"TCS": good}
        if symbols == ["INFY"]:
            return {"INFY": good}
        return {}

    service._download_batch = fake_download  # type: ignore[method-assign]

    frames = service.load_history(["TCS", "INFY"], use_cache=False)

    assert sorted(frames) == ["INFY", "TCS"]
    assert calls == [["TCS", "INFY"], ["INFY"]]


def test_load_history_does_not_retry_when_the_whole_batch_fails(temp_config) -> None:
    service = PriceService(temp_config)
    calls: list[list[str]] = []

    def fake_download(symbols: list[str], days: int):
        calls.append(list(symbols))
        return {}

    service._download_batch = fake_download  # type: ignore[method-assign]

    frames = service.load_history(["TCS", "INFY"], use_cache=False)

    assert frames == {}
    assert calls == [["TCS", "INFY"]]
