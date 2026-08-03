"""Tests for Alpaca broker client."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.broker.alpaca import AlpacaClient
from src.broker.base import OrderRequest
from src.config import Config, Secrets


@pytest.fixture
def alpaca_config():
    secrets = Secrets(
        alpaca_api_key="test-key",
        alpaca_secret_key="test-secret",
        alpaca_paper=True,
    )
    raw = {
        "broker": {"provider": "alpaca"},
        "market": {"region": "US"},
        "alpaca": {"min_call_interval_seconds": 0},
    }
    return Config(raw=raw, secrets=secrets)


def test_normalize_symbol_strips_ns_suffix():
    assert AlpacaClient.normalize_symbol("aapl.ns") == "AAPL"
    assert AlpacaClient.normalize_symbol("MSFT") == "MSFT"


def test_connect_reads_account(alpaca_config):
    client = AlpacaClient(alpaca_config)
    with patch.object(client, "_trading", return_value={"status": "ACTIVE", "equity": "100000"}):
        assert client.connect() is True


def test_get_holdings_maps_positions(alpaca_config):
    client = AlpacaClient(alpaca_config)
    rows = [
        {
            "symbol": "AAPL",
            "qty": "10",
            "avg_entry_price": "150.5",
            "current_price": "155.0",
        }
    ]
    with patch.object(client, "_trading", return_value=rows):
        holdings = client.get_holdings()
    assert len(holdings) == 1
    assert holdings[0].symbol == "AAPL"
    assert holdings[0].quantity == 10
    assert holdings[0].exchange == "US"


def test_place_limit_order(alpaca_config):
    client = AlpacaClient(alpaca_config)
    with patch.object(
        client,
        "_trading",
        return_value={"id": "ord-1", "status": "accepted"},
    ) as mock_trading:
        result = client.place_order(
            OrderRequest(
                symbol="AAPL",
                side="BUY",
                quantity=5,
                order_type="LIMIT",
                price=150.25,
            )
        )
    assert result.success is True
    assert result.order_id == "ord-1"
    body = mock_trading.call_args.kwargs.get("json_body") or mock_trading.call_args[1].get("json_body")
    assert body["symbol"] == "AAPL"
    assert body["limit_price"] == "150.25"


def test_get_last_prices_from_snapshots(alpaca_config):
    client = AlpacaClient(alpaca_config)
    payload = {"AAPL": {"latestTrade": {"p": 190.5}}}
    with patch.object(client, "_data", return_value=payload):
        prices = client.get_last_prices(["AAPL"])
    assert prices["AAPL"] == 190.5
