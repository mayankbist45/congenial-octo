"""Broker selection.

Live Angel One is used for India when credentials exist and ``DRY_RUN`` is off.
Alpaca (paper or live) is used when ``broker.provider: alpaca`` is set on the
persona. In India paper mode the journal is the book of record; Alpaca paper
accounts hold the US book directly on Alpaca's servers.
"""

from __future__ import annotations

from typing import Optional

from src.broker.base import BrokerClient
from src.config import Config, get_config
from src.logging_setup import get_logger

logger = get_logger(__name__)


def _is_alpaca(config: Config) -> bool:
    return config.broker_provider == "alpaca"


def build_broker(
    config: Optional[Config] = None,
    force_simulated: bool = False,
) -> BrokerClient:
    config = config or get_config()
    secrets = config.secrets

    if _is_alpaca(config):
        if force_simulated or not secrets.has_alpaca_credentials:
            from src.broker.simulated import SimulatedBroker
            from src.market_data.prices import PriceService

            logger.info("Using simulated broker (Alpaca credentials missing or forced)")
            return SimulatedBroker(config, price_lookup=PriceService(config))

        from src.broker.alpaca import AlpacaClient

        if secrets.dry_run and not secrets.alpaca_paper:
            logger.warning(
                "DRY_RUN=true blocks live Alpaca; use ALPACA_PAPER=true for paper trading"
            )
            from src.broker.simulated import SimulatedBroker
            from src.market_data.prices import PriceService

            return SimulatedBroker(config, price_lookup=PriceService(config))

        mode = "paper" if secrets.alpaca_paper else "live"
        logger.info("Using Alpaca broker (%s)", mode)
        return AlpacaClient(config)

    use_live = (
        not force_simulated and secrets.has_broker_credentials and not secrets.dry_run
    )

    if use_live:
        from src.broker.angelone import AngelOneClient

        logger.info("Using live Angel One broker (real orders enabled)")
        return AngelOneClient(config)

    from src.broker.simulated import SimulatedBroker
    from src.market_data.prices import PriceService

    if secrets.has_broker_credentials and secrets.dry_run:
        logger.info(
            "Using simulated broker for the paper book "
            "(DRY_RUN=true; Angel One credentials ignored for holdings/cash)"
        )
    else:
        logger.info("Using simulated broker (no live Angel One session)")
    return SimulatedBroker(config, price_lookup=PriceService(config))


def build_market_data_broker(
    config: Optional[Config] = None,
    force_simulated: bool = False,
) -> BrokerClient:
    """Broker used for candles/quotes.

    Unlike :func:`build_broker`, this may open Angel One or Alpaca even when
    ``DRY_RUN`` is true, because paper books still need live market data.
    """
    config = config or get_config()
    secrets = config.secrets

    if _is_alpaca(config):
        if force_simulated or not secrets.has_alpaca_credentials:
            from src.broker.simulated import SimulatedBroker
            from src.market_data.prices import PriceService

            logger.info("Using simulated broker for US market data")
            return SimulatedBroker(config, price_lookup=PriceService(config))

        from src.broker.alpaca import AlpacaClient

        logger.info(
            "Using Alpaca for market data"
            + (" (paper account)" if secrets.alpaca_paper else "")
        )
        return AlpacaClient(config)

    if force_simulated or not secrets.has_broker_credentials:
        from src.broker.simulated import SimulatedBroker
        from src.market_data.prices import PriceService

        logger.info("Using simulated broker for intraday market data")
        return SimulatedBroker(config, price_lookup=PriceService(config))

    from src.broker.angelone import AngelOneClient
    from src.broker.candle_pool import AngelOneCandlePool

    primary = AngelOneClient(config)
    secondary_creds = secrets.secondary_candle_credentials()
    if secondary_creds and secondary_creds.is_complete:
        secondary = AngelOneClient(config, credentials=secondary_creds)
        logger.info(
            "Using Angel One candle pool (primary + data2 API keys) for intraday data"
            + (" (paper trading; orders not routed here)" if secrets.dry_run else "")
        )
        return AngelOneCandlePool([primary, secondary], config=config)

    logger.info(
        "Using Angel One for market data"
        + (
            " (paper trading; orders not routed here)"
            if secrets.dry_run
            else ""
        )
    )
    return primary
