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


def _angelone_secrets(**overrides) -> Secrets:
    base = dict(
        angelone_api_key="primary-key",
        angelone_client_code="P1",
        angelone_pin="1111",
        angelone_totp_secret="PRIMARYSECRET11",
        angelone_data2_api_key="data2-key",
        angelone_data2_client_code="P2",
        angelone_data2_pin="2222",
        angelone_data2_totp_secret="DATA2SECRET2222",
        dry_run=True,
    )
    base.update(overrides)
    return Secrets(**base)


def test_swing_broker_uses_data2_key():
    from src.broker.dry_run import DryRunBroker
    from src.connectors.angelone import _book_credentials
    from src.connectors.registry import build_broker as build

    config = Config(raw={}, secrets=_angelone_secrets())
    creds = _book_credentials(config)
    assert creds is not None
    assert creds.label == "data2"
    assert creds.api_key == "data2-key"

    broker = build(config)
    assert isinstance(broker, DryRunBroker)
    assert broker._inner.credential_label == "data2"


def test_intraday_broker_stays_on_primary_key():
    from src.broker.dry_run import DryRunBroker
    from src.connectors.angelone import _book_credentials
    from src.connectors.registry import build_broker as build

    config = Config(
        raw={"intraday": {"enabled": True}},
        secrets=_angelone_secrets(),
    )
    creds = _book_credentials(config)
    assert creds is not None
    assert creds.label == "primary"
    assert creds.api_key == "primary-key"

    broker = build(config)
    assert isinstance(broker, DryRunBroker)
    assert broker._inner.credential_label == "primary"


def test_intraday_market_data_uses_primary_not_pool_by_default():
    from src.broker.candle_budget import CandleBudgetCoordinator
    from src.connectors.registry import build_market_data_broker

    config = Config(
        raw={"intraday": {"enabled": True}, "angelone": {"use_candle_pool": False}},
        secrets=_angelone_secrets(),
    )
    broker = build_market_data_broker(config)
    assert isinstance(broker, CandleBudgetCoordinator)
    assert broker._inner.credential_label == "primary"
