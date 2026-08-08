"""Tests for Alpaca crypto broker client."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from src.broker.alpaca_crypto import AlpacaCryptoClient
from src.broker.base import OrderRequest
from src.config import Config, Secrets


@pytest.fixture
def alpaca_crypto_config():
    secrets = Secrets(
        alpaca_api_key="test-key",
        alpaca_secret_key="test-secret",
        alpaca_paper=True,
    )
    raw = {
        "connectors": {"broker": "alpaca_crypto"},
        "market": {"region": "US"},
        "alpaca": {"min_call_interval_seconds": 0},
    }
    return Config(raw=raw, secrets=secrets)


def test_normalize_symbol_formats_slash_pair(alpaca_crypto_config):
    assert AlpacaCryptoClient.normalize_symbol("btcusd") == "BTC/USD"
    assert AlpacaCryptoClient.normalize_symbol("eth-usd") == "ETH/USD"


def test_get_candles_maps_crypto_payload(alpaca_crypto_config):
    client = AlpacaCryptoClient(alpaca_crypto_config)
    payload = {
        "bars": {
            "BTC/USD": [
                {
                    "t": "2026-08-07T13:35:00Z",
                    "o": 114000.0,
                    "h": 114200.0,
                    "l": 113900.0,
                    "c": 114150.0,
                    "v": 12.5,
                }
            ]
        }
    }
    with patch.object(client, "_data", return_value=payload):
        rows = client.get_candles(
            "BTC/USD",
            datetime(2026, 8, 7, 13, 30, tzinfo=timezone.utc),
            datetime(2026, 8, 7, 14, 0, tzinfo=timezone.utc),
            interval="FIVE_MINUTE",
        )
    assert rows[0]["close"] == 114150.0
    assert rows[0]["volume"] == 12.5


def test_place_market_order_uses_fractional_qty(alpaca_crypto_config):
    client = AlpacaCryptoClient(alpaca_crypto_config)
    with patch.object(
        client,
        "_trading",
        return_value={"id": "ord-crypto-1", "status": "accepted"},
    ) as mock_trading:
        result = client.place_order(
            OrderRequest(
                symbol="BTC/USD",
                side="BUY",
                quantity=0.125,
                order_type="MARKET",
            )
        )
    assert result.success is True
    body = mock_trading.call_args.kwargs.get("json_body") or mock_trading.call_args[1].get("json_body")
    assert body["symbol"] == "BTC/USD"
    assert body["qty"] == "0.125"
    assert body["time_in_force"] == "gtc"


def test_place_market_order_allows_notional(alpaca_crypto_config):
    client = AlpacaCryptoClient(alpaca_crypto_config)
    with patch.object(
        client,
        "_trading",
        return_value={"id": "ord-crypto-2", "status": "accepted"},
    ) as mock_trading:
        result = client.place_order(
            OrderRequest(
                symbol="ETH/USD",
                side="BUY",
                notional=250.0,
                order_type="MARKET",
            )
        )
    assert result.success is True
    body = mock_trading.call_args.kwargs.get("json_body") or mock_trading.call_args[1].get("json_body")
    assert body["notional"] == "250"
    assert "qty" not in body
