"""Broker selection.

The fund uses the live Angel One client when credentials exist and ``DRY_RUN``
is off; otherwise it falls back to the simulated broker. That fallback is what
lets the whole pipeline run in read-only mode during Phase 1.
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

    # Read-only Angel One access is still valuable in dry-run mode: real
    # holdings and real cash, but orders are simulated.
    if not force_simulated and secrets.has_broker_credentials and secrets.dry_run:
        from src.broker.angelone import AngelOneClient
        from src.broker.dry_run import DryRunBroker

        logger.info(
            "Using Angel One for read-only data with order placement disabled "
            "(DRY_RUN=true)"
        )
        return DryRunBroker(AngelOneClient(config))

    from src.broker.simulated import SimulatedBroker
    from src.market_data.prices import PriceService

    logger.info("Using simulated broker (no Angel One credentials configured)")
    return SimulatedBroker(config, price_lookup=PriceService(config))
