"""Market scheduling helpers."""

from __future__ import annotations

from src.config import enabled_personas, get_config, get_persona_config


def enabled_intraday_personas(market_id: str) -> list[str]:
    base = get_config()
    enabled = set(enabled_personas())
    result: list[str] = []
    for pid in enabled:
        try:
            pcfg = get_persona_config(pid, base=base)
        except ValueError:
            continue
        if pcfg.market_id != market_id:
            continue
        if not pcfg.get("intraday.enabled"):
            continue
        result.append(pid)
    return sorted(result)
