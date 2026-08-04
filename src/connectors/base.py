"""Broker connector protocol."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from src.broker.base import BrokerClient
from src.config import Config


class BrokerConnector(ABC):
    """Pluggable adapter from policy config to a :class:`BrokerClient`."""

    @property
    @abstractmethod
    def provider_id(self) -> str:
        """Stable id used in settings, e.g. ``angelone`` or ``alpaca``."""

    @property
    @abstractmethod
    def label(self) -> str:
        """Human-readable name for logs."""

    @abstractmethod
    def has_credentials(self, config: Config) -> bool:
        """Whether this connector can authenticate."""

    @abstractmethod
    def build_broker(
        self,
        config: Config,
        *,
        force_simulated: bool = False,
    ) -> BrokerClient:
        """Book-of-record broker (orders + holdings)."""

    def build_market_data_broker(
        self,
        config: Config,
        *,
        force_simulated: bool = False,
    ) -> BrokerClient:
        """Quotes/candles broker; defaults to the same client as the book."""
        return self.build_broker(config, force_simulated=force_simulated)
