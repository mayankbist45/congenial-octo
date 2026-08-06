"""Tests for Angel One dual-key candle pool."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

from src.broker.angelone import AngelOneClient, AngelOneCredentials
from src.broker.base import BrokerError
from src.broker.candle_budget import CandleBudgetCoordinator
from src.broker.candle_pool import AngelOneCandlePool
from src.config import Config, Secrets


def _mock_config() -> Config:
    secrets = Secrets(
        angelone_api_key="k1",
        angelone_client_code="C1",
        angelone_pin="1234",
        angelone_totp_secret="SECRET1",
        angelone_data2_api_key="k2",
    )
    return Config(
        raw={"angelone": {"rate_limit_backoff_seconds": 60.0}},
        secrets=secrets,
    )


def test_secondary_credentials_reuse_primary_login():
    secrets = Secrets(
        angelone_api_key="k1",
        angelone_client_code="C1",
        angelone_pin="1234",
        angelone_totp_secret="SECRET1",
        angelone_data2_api_key="k2",
    )
    creds = secrets.secondary_candle_credentials()
    assert creds is not None
    assert creds.api_key == "k2"
    assert creds.client_code == "C1"
    assert creds.label == "data2"


def test_pool_failover_on_rate_limit():
    config = _mock_config()
    primary = MagicMock(spec=AngelOneClient)
    primary.credential_label = "primary"
    primary.was_rate_limited = AngelOneClient.was_rate_limited
    secondary = MagicMock(spec=AngelOneClient)
    secondary.credential_label = "data2"
    secondary.was_rate_limited = AngelOneClient.was_rate_limited

    primary.get_candles.side_effect = BrokerError(
        "getCandleData failed [primary]: Access denied because of exceeding access rate"
    )
    secondary.get_candles.return_value = [{"timestamp": "2026-08-03 09:15", "close": 100.0}]

    pool = AngelOneCandlePool([primary, secondary], config=config)
    rows = pool.get_candles(
        "RELIANCE",
        datetime(2026, 8, 3, 9, 15),
        datetime(2026, 8, 3, 15, 30),
        "FIVE_MINUTE",
    )

    assert len(rows) == 1
    primary.get_candles.assert_called_once()
    secondary.get_candles.assert_called_once()


def test_pool_primary_first_on_success():
    config = _mock_config()
    primary = MagicMock(spec=AngelOneClient)
    primary.credential_label = "primary"
    primary.was_rate_limited = AngelOneClient.was_rate_limited
    secondary = MagicMock(spec=AngelOneClient)
    secondary.credential_label = "data2"
    secondary.was_rate_limited = AngelOneClient.was_rate_limited

    row = [{"timestamp": "2026-08-03 09:15", "close": 100.0}]
    primary.get_candles.return_value = row

    pool = AngelOneCandlePool([primary, secondary], config=config)
    args = (
        datetime(2026, 8, 3, 9, 15),
        datetime(2026, 8, 3, 15, 30),
        "FIVE_MINUTE",
    )

    for symbol in ("RELIANCE", "TCS", "INFY", "HDFCBANK"):
        pool.get_candles(symbol, *args)

    assert primary.get_candles.call_count == 4
    secondary.get_candles.assert_not_called()


def test_was_rate_limited_detects_angel_message():
    assert AngelOneClient.was_rate_limited("Access denied because of exceeding access rate")
    assert not AngelOneClient.was_rate_limited("invalid symbol token")


def test_budget_coordinator_enforces_spacing(monkeypatch):
    config = _mock_config()
    config.raw["angelone"]["candle_budget_spacing_seconds"] = 0.05
    inner = MagicMock()
    inner.name = "MockBroker"
    inner.get_candles.return_value = [{"timestamp": "2026-08-03 09:15", "close": 100.0}]
    inner.connect.return_value = True

    sleeps: list[float] = []
    monkeypatch.setattr("src.broker.candle_budget.time.sleep", lambda s: sleeps.append(s))

    coord = CandleBudgetCoordinator(inner, config=config)
    args = (
        datetime(2026, 8, 3, 9, 15),
        datetime(2026, 8, 3, 15, 30),
        "FIVE_MINUTE",
    )
    coord.get_candles("RELIANCE", *args)
    coord.get_candles("TCS", *args)

    assert inner.get_candles.call_count == 2
    assert len(sleeps) >= 1
