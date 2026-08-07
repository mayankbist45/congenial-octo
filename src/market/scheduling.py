"""Market scheduling helpers."""

from __future__ import annotations

from typing import List, Optional

from src.config import enabled_personas, get_config, get_persona_config
from src.market.registry import get_market


def enabled_intraday_personas(market_id: str) -> List[str]:
    """Personas started by the market intraday watch unit.

    Prefer the explicit list in config/markets/<id>.yaml scheduling.intraday_watch
    (when present) so India does not launch every enabled paper persona and
    hammer Angel One. Fall back to all enabled intraday personas for the market.
    """
    base = get_config()
    enabled = set(enabled_personas())
    try:
        market = get_market(market_id)
    except ValueError:
        market = None

    scheduled: Optional[List[str]] = None
    if market and market.scheduling.intraday_watch:
        scheduled = list(market.scheduling.intraday_watch.personas)

    candidates = scheduled if scheduled else sorted(enabled)
    result: List[str] = []
    for pid in candidates:
        if pid not in enabled:
            continue
        try:
            pcfg = get_persona_config(pid, base=base)
        except ValueError:
            continue
        if pcfg.market_id != market_id:
            continue
        if not pcfg.get("intraday.enabled"):
            continue
        result.append(pid)
    return result
