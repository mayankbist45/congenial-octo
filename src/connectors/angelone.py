"""Angel One connector — default India broker (unchanged behaviour)."""

from __future__ import annotations

from src.broker.base import BrokerClient
from src.config import Config
from src.connectors.base import BrokerConnector
from src.logging_setup import get_logger
from src.market_data.prices import PriceService

logger = get_logger(__name__)


class AngelOneConnector(BrokerConnector):
    @property
    def provider_id(self) -> str:
        return "angelone"

    @property
    def label(self) -> str:
        return "Angel One SmartAPI"

    def has_credentials(self, config: Config) -> bool:
        return config.secrets.has_broker_credentials

    def build_broker(
        self,
        config: Config,
        *,
        force_simulated: bool = False,
    ) -> BrokerClient:
        secrets = config.secrets
        use_live = (
            not force_simulated and secrets.has_broker_credentials and not secrets.dry_run
        )

        if use_live:
            from src.broker.angelone import AngelOneClient

            logger.info("Using live Angel One broker (real orders enabled)")
            return AngelOneClient(config)

        if not force_simulated and secrets.has_broker_credentials and secrets.dry_run:
            from src.broker.angelone import AngelOneClient
            from src.broker.dry_run import DryRunBroker

            logger.info(
                "Using Angel One for read-only data with order placement disabled "
                "(DRY_RUN=true)"
            )
            return DryRunBroker(AngelOneClient(config))

        from src.broker.simulated import SimulatedBroker

        logger.info("Using simulated broker (no Angel One credentials configured)")
        return SimulatedBroker(config, price_lookup=PriceService(config))

    def build_market_data_broker(
        self,
        config: Config,
        *,
        force_simulated: bool = False,
    ) -> BrokerClient:
        """Angel One for live NSE quotes even when the book is simulated."""
        secrets = config.secrets
        if force_simulated or not secrets.has_broker_credentials:
            from src.broker.simulated import SimulatedBroker

            logger.info("Using simulated broker for market data")
            return SimulatedBroker(config, price_lookup=PriceService(config))

        from src.broker.angelone import AngelOneClient
        from src.broker.candle_pool import AngelOneCandlePool

        primary = AngelOneClient(config)
        secondary_creds = secrets.secondary_candle_credentials()
        if secondary_creds and secondary_creds.is_complete:
            secondary = AngelOneClient(config, credentials=secondary_creds)
            logger.info(
                "Using Angel One candle pool (primary + data2 API keys) for intraday data"
                + (" (DRY_RUN; orders not routed here)" if secrets.dry_run else "")
            )
            return AngelOneCandlePool([primary, secondary], config=config)

        logger.info(
            "Using Angel One for market data"
            + (" (DRY_RUN; orders not routed here)" if secrets.dry_run else "")
        )
        return primary
