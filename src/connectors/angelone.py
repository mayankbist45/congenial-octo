"""Angel One connector — default India broker.

Key split (when ``ANGELONE_DATA2_*`` is set):
- Swing books (``build_broker``) prefer the data2 SmartAPI app.
- Intraday market data (``build_market_data_broker``) stays on the primary app.
This keeps close/analyse rate limits off the intraday candle quota.
"""

from __future__ import annotations

from typing import Optional

from src.broker.base import BrokerClient
from src.broker.angelone import AngelOneClient, AngelOneCredentials
from src.config import Config
from src.connectors.base import BrokerConnector
from src.logging_setup import get_logger
from src.market_data.prices import PriceService

logger = get_logger(__name__)


def _is_intraday_persona(config: Config) -> bool:
    return bool(config.get("intraday.enabled"))


def _primary_credentials(config: Config) -> AngelOneCredentials:
    return AngelOneClient._credentials_from_secrets(config.secrets)


def _swing_credentials(config: Config) -> Optional[AngelOneCredentials]:
    """Prefer data2 for swing; fall back to primary when data2 is incomplete."""
    secondary = config.secrets.secondary_candle_credentials()
    if secondary is not None and secondary.is_complete:
        return secondary
    if config.secrets.has_broker_credentials:
        return _primary_credentials(config)
    return None


def _book_credentials(config: Config) -> Optional[AngelOneCredentials]:
    if _is_intraday_persona(config):
        if config.secrets.has_broker_credentials:
            return _primary_credentials(config)
        return None
    return _swing_credentials(config)


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
        creds = _book_credentials(config)
        use_live = (
            not force_simulated and creds is not None and creds.is_complete and not secrets.dry_run
        )

        if use_live:
            assert creds is not None
            logger.info(
                "Using live Angel One broker [%s] (real orders enabled)",
                creds.label,
            )
            return AngelOneClient(config, credentials=creds)

        if not force_simulated and creds is not None and creds.is_complete and secrets.dry_run:
            from src.broker.dry_run import DryRunBroker

            logger.info(
                "Using Angel One [%s] for read-only data with order placement disabled "
                "(DRY_RUN=true)",
                creds.label,
            )
            return DryRunBroker(AngelOneClient(config, credentials=creds))

        from src.broker.simulated import SimulatedBroker

        logger.info("Using simulated broker (no Angel One credentials configured)")
        return SimulatedBroker(config, price_lookup=PriceService(config))

    def build_market_data_broker(
        self,
        config: Config,
        *,
        force_simulated: bool = False,
    ) -> BrokerClient:
        """Angel One for live NSE quotes even when the book is simulated.

        Intraday always uses the primary key. Optional candle pool can still
        share data2 for candles only when explicitly enabled — by default data2
        is reserved for swing ``build_broker`` traffic.
        """
        secrets = config.secrets
        if force_simulated or not secrets.has_broker_credentials:
            from src.broker.simulated import SimulatedBroker

            logger.info("Using simulated broker for market data")
            return SimulatedBroker(config, price_lookup=PriceService(config))

        from src.broker.candle_budget import CandleBudgetCoordinator
        from src.broker.candle_pool import AngelOneCandlePool

        primary = AngelOneClient(config, credentials=_primary_credentials(config))
        secondary_creds = secrets.secondary_candle_credentials()
        use_pool = bool(config.get("angelone.use_candle_pool", False))
        if use_pool and secondary_creds and secondary_creds.is_complete:
            secondary = AngelOneClient(config, credentials=secondary_creds)
            logger.info(
                "Using Angel One candle pool (primary + data2 API keys) for intraday data"
                + (" (DRY_RUN; orders not routed here)" if secrets.dry_run else "")
            )
            inner = AngelOneCandlePool([primary, secondary], config=config)
        else:
            if secondary_creds and secondary_creds.is_complete and not use_pool:
                logger.info(
                    "Using Angel One [primary] for intraday market data; "
                    "data2 reserved for swing books"
                )
            else:
                logger.info(
                    "Using Angel One [primary] for market data"
                    + (" (DRY_RUN; orders not routed here)" if secrets.dry_run else "")
                )
            inner = primary
        return CandleBudgetCoordinator(inner, config=config)
