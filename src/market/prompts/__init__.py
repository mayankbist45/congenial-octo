"""Market-specific prompt bundles."""

from __future__ import annotations

from src.config import Config
from src.intraday.prompts import PersonaPromptBundle, get_persona_prompt_bundle
from src.market.registry import persona_strategy_type


def get_market_prompt_bundle(config: Config) -> PersonaPromptBundle:
    """Return prompt bundle for persona, using strategy type not market suffix."""
    strategy = persona_strategy_type(config.persona_id)
    return get_persona_prompt_bundle(strategy)
