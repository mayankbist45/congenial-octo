"""Broker selection.

Live Angel One is used only when credentials exist and ``DRY_RUN`` is off.
In paper mode the book of record is the journal (per persona), so the simulated
broker is used even if Angel One credentials are present. Angel One can still
be used elsewhere for market data; it must not define the paper portfolio.
"""

from __future__ import annotations

from typing import Optional

from src.broker.base import BrokerClient
from src.config import Config, get_config
from src.logging_setup import get_logger

logger = get_logger(__name__)


def build_broker(
    config: Optional[Config] = None,
    force_simulated: bool = False,
) -> BrokerClient:
    config = config or get_config()
    secrets = config.secrets

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

    Unlike :func:`build_broker`, this may open Angel One even when ``DRY_RUN``
    is true, because paper books still need live intraday market data. Orders
    placed through this client must not be used for live trading in paper mode;
    Calan's watch/simulator keep fills in their own paper state.

    When ``ANGELONE_DATA2_*`` credentials are set, a dual-key candle pool
    spreads ``getCandleData`` quota across two SmartAPI apps.
    """
    config = config or get_config()
    secrets = config.secrets

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
