"""Alpaca Markets connector for spot crypto paper/live APIs."""

from __future__ import annotations

from src.broker.base import BrokerClient
from src.config import Config
from src.connectors.base import BrokerConnector
from src.logging_setup import get_logger

logger = get_logger(__name__)


class AlpacaCryptoConnector(BrokerConnector):
    @property
    def provider_id(self) -> str:
        return "alpaca_crypto"

    @property
    def label(self) -> str:
        return "Alpaca Crypto"

    def build_broker(
        self,
        config: Config,
        *,
        force_simulated: bool = False,
    ) -> BrokerClient:
        if force_simulated or not config.secrets.has_alpaca_credentials:
            from src.broker.simulated import SimulatedBroker

            logger.info(
                "Using simulated broker (Alpaca crypto credentials missing or forced)"
            )
            return SimulatedBroker(config)

        from src.broker.alpaca_crypto import AlpacaCryptoClient

        mode = "paper" if config.secrets.alpaca_paper else "live"
        logger.info("Using Alpaca crypto broker (%s)", mode)
        return AlpacaCryptoClient(config)
