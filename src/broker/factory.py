"""Broker selection — delegates to the connector registry.

Angel One remains the default connector for India. Alpaca and future brokers
register in :mod:`src.connectors.registry` and are selected via
``connectors.broker`` in settings (or ``broker.provider`` when personas exist).
"""

from __future__ import annotations

from typing import Optional

from src.broker.base import BrokerClient
from src.config import Config, get_config
from src.connectors.registry import build_broker as _build_broker
from src.connectors.registry import build_market_data_broker as _build_market_data_broker

__all__ = ["build_broker", "build_market_data_broker"]


def build_broker(
    config: Optional[Config] = None,
    force_simulated: bool = False,
) -> BrokerClient:
    return _build_broker(config=config or get_config(), force_simulated=force_simulated)


def build_market_data_broker(
    config: Optional[Config] = None,
    force_simulated: bool = False,
) -> BrokerClient:
    return _build_market_data_broker(
        config=config or get_config(), force_simulated=force_simulated
    )
