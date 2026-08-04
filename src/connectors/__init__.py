"""External broker and market-data connectors.

Connectors adapt third-party APIs (Angel One, Alpaca, …) to :class:`~src.broker.base.BrokerClient`
without replacing one another. The fund defaults to Angel One for India; set
``connectors.broker: alpaca`` in settings (or per-persona ``broker.provider``)
to route a book through a different connector.
"""

from src.connectors.registry import (
    build_broker,
    build_market_data_broker,
    get_connector,
    list_connectors,
)

__all__ = [
    "build_broker",
    "build_market_data_broker",
    "get_connector",
    "list_connectors",
]
