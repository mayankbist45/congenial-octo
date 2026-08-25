"""Broker connector registry and factory helpers."""

from __future__ import annotations

from typing import Dict, List, Optional

from src.broker.base import BrokerClient
from src.config import Config, get_config
from src.connectors.alpaca import AlpacaConnector
from src.connectors.alpaca_crypto import AlpacaCryptoConnector
from src.connectors.angelone import AngelOneConnector
from src.connectors.base import BrokerConnector
from src.logging_setup import get_logger

logger = get_logger(__name__)

_CONNECTORS = (AngelOneConnector(), AlpacaConnector(), AlpacaCryptoConnector())
_REGISTRY: Dict[str, BrokerConnector] = {connector.provider_id: connector for connector in _CONNECTORS}

DEFAULT_PROVIDER = "angelone"


def list_connectors() -> List[str]:
    return list(_REGISTRY.keys())


def get_connector(provider: Optional[str] = None, config: Optional[Config] = None) -> BrokerConnector:
    """Resolve a connector by explicit id or from config."""
    config = config or get_config()
    provider_id = (provider or config.broker_provider or DEFAULT_PROVIDER).lower().strip()
    connector = _REGISTRY.get(provider_id)
    if connector is None:
        available = ", ".join(sorted(_REGISTRY))
        raise ValueError(
            f"Unknown broker connector {provider_id!r}; available: {available}"
        )
    return connector


def build_broker(
    config: Optional[Config] = None,
    force_simulated: bool = False,
    provider: Optional[str] = None,
) -> BrokerClient:
    config = config or get_config()
    connector = get_connector(provider, config)
    logger.debug("Building book broker via %s connector", connector.provider_id)
    return connector.build_broker(config, force_simulated=force_simulated)


def build_market_data_broker(
    config: Optional[Config] = None,
    force_simulated: bool = False,
    provider: Optional[str] = None,
) -> BrokerClient:
    config = config or get_config()
    connector = get_connector(provider, config)
    logger.debug("Building market-data broker via %s connector", connector.provider_id)
    return connector.build_market_data_broker(config, force_simulated=force_simulated)
