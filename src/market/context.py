"""Runtime market context for a persona run."""

from __future__ import annotations

from dataclasses import dataclass

from src.config import Config, get_persona_config
from src.market.calendar import SessionCalendar
from src.market.registry import Market, get_market


@dataclass(frozen=True)
class MarketContext:
    """Resolved market + persona runtime bundle."""

    market: Market
    config: Config
    calendar: SessionCalendar

    @property
    def market_id(self) -> str:
        return self.market.id

    @property
    def legacy_region(self) -> str:
        return self.market.legacy_region

    @property
    def currency(self) -> str:
        return self.market.currency


def build_market_context(config: Config) -> MarketContext:
    market = get_market(config.market_id)
    return MarketContext(
        market=market,
        config=config,
        calendar=SessionCalendar(market),
    )


def market_context_for_persona(
    persona_id: str,
    settings_path: str | None = None,
    base: Config | None = None,
) -> MarketContext:
    config = get_persona_config(persona_id, settings_path=settings_path, base=base)
    return build_market_context(config)
