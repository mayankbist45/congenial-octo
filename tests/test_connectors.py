"""Tests for broker connector registry."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.config import Config, Secrets
from src.connectors.registry import build_broker, get_connector, list_connectors


def test_list_connectors_includes_angelone_and_alpaca():
    ids = list_connectors()
    assert "angelone" in ids
    assert "alpaca" in ids
    assert "alpaca_crypto" in ids


def test_default_connector_is_angelone():
    config = Config(raw={}, secrets=Secrets())
    assert get_connector(config=config).provider_id == "angelone"


def test_alpaca_connector_selected_from_settings():
    config = Config(
        raw={"connectors": {"broker": "alpaca"}},
        secrets=Secrets(alpaca_api_key="k", alpaca_secret_key="s"),
    )
    assert get_connector(config=config).provider_id == "alpaca"


def test_alpaca_crypto_connector_selected_from_settings():
    config = Config(
        raw={"connectors": {"broker": "alpaca_crypto"}},
        secrets=Secrets(alpaca_api_key="k", alpaca_secret_key="s"),
    )
    assert get_connector(config=config).provider_id == "alpaca_crypto"


def test_build_broker_angelone_unchanged_without_credentials():
    config = Config(raw={}, secrets=Secrets())
    broker = build_broker(config)
    assert broker.name == "Simulated"


def test_build_broker_alpaca_when_configured():
    config = Config(
        raw={"connectors": {"broker": "alpaca"}},
        secrets=Secrets(alpaca_api_key="k", alpaca_secret_key="s", alpaca_paper=True),
    )
    with patch("src.broker.alpaca.AlpacaClient") as mock_cls:
        mock_cls.return_value = MagicMock(name="Alpaca[Paper]")
        broker = build_broker(config)
    assert mock_cls.called


def test_build_broker_alpaca_crypto_when_configured():
    config = Config(
        raw={"connectors": {"broker": "alpaca_crypto"}},
        secrets=Secrets(alpaca_api_key="k", alpaca_secret_key="s", alpaca_paper=True),
    )
    with patch("src.broker.alpaca_crypto.AlpacaCryptoClient") as mock_cls:
        mock_cls.return_value = MagicMock(name="AlpacaCrypto[Paper]")
        broker = build_broker(config)
    assert mock_cls.called


def test_unknown_connector_raises():
    config = Config(raw={"connectors": {"broker": "unknown"}}, secrets=Secrets())
    with pytest.raises(ValueError, match="Unknown broker connector"):
        get_connector(config=config)
