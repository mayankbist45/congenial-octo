"""Alpaca Markets connector — US equities paper/live (optional)."""

from __future__ import annotations

from src.broker.base import BrokerClient
from src.config import Config
from src.connectors.base import BrokerConnector
from src.logging_setup import get_logger
from src.market_data.prices import PriceService

logger = get_logger(__name__)


class AlpacaConnector(BrokerConnector):
    @property
    def provider_id(self) -> str:
        return "alpaca"

    @property
    def label(self) -> str:
        return "Alpaca Markets"

    def has_credentials(self, config: Config) -> bool:
        return config.secrets.has_alpaca_credentials

    def build_broker(
        self,
        config: Config,
        *,
        force_simulated: bool = False,
    ) -> BrokerClient:
        secrets = config.secrets
        if force_simulated or not secrets.has_alpaca_credentials:
            from src.broker.simulated import SimulatedBroker

            logger.info("Using simulated broker (Alpaca credentials missing or forced)")
            return SimulatedBroker(config, price_lookup=PriceService(config))

        if secrets.dry_run and not secrets.alpaca_paper:
            logger.warning(
                "DRY_RUN=true blocks live Alpaca; set ALPACA_PAPER=true for paper trading"
            )
            from src.broker.simulated import SimulatedBroker

            return SimulatedBroker(config, price_lookup=PriceService(config))

        from src.broker.alpaca import AlpacaClient

        mode = "paper" if secrets.alpaca_paper else "live"
        logger.info("Using Alpaca broker (%s)", mode)
        return AlpacaClient(config)

    def build_market_data_broker(
        self,
        config: Config,
        *,
        force_simulated: bool = False,
    ) -> BrokerClient:
        return self.build_broker(config, force_simulated=force_simulated)
