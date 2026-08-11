"""Dashboard presentation helpers driven by the market registry."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from src.config import Config, get_persona_config
from src.market.registry import Market, get_market, list_markets


def market_label(market_id: str) -> str:
    try:
        return get_market(market_id).label
    except ValueError:
        return market_id


def market_badge_class(market_id: str) -> str:
    try:
        return get_market(market_id).display.badge_class
    except ValueError:
        return "region-in"


def market_exchange_label(market_id: str) -> str:
    try:
        return get_market(market_id).display.exchange_label
    except ValueError:
        return ""


def market_broker_label(market_id: str) -> str:
    try:
        return get_market(market_id).display.broker_label
    except ValueError:
        return ""


def format_market_timestamp(
    value: Optional[str | datetime],
    market_id: str,
) -> str:
    if not value:
        return "-"
    from src.market.calendar import calendar_for_market

    cal = calendar_for_market(market_id)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    else:
        dt = value
    return cal.format_local(dt)


_MARKET_DISPLAY_ORDER = ("india_equity", "us_equity", "crypto_spot_us")


def all_markets_summary() -> List[Dict[str, Any]]:
    by_id = {market.id: market for market in list_markets()}
    ordered: List[Market] = []
    for market_id in _MARKET_DISPLAY_ORDER:
        market = by_id.pop(market_id, None)
        if market is not None:
            ordered.append(market)
    ordered.extend(by_id[key] for key in sorted(by_id))
    return [
        {
            "id": market.id,
            "label": market.label,
            "legacy_region": market.legacy_region,
            "currency": market.currency,
            "badge_class": market.display.badge_class,
            "exchange_label": market.display.exchange_label,
            "broker_label": market.display.broker_label,
        }
        for market in ordered
    ]


def persona_market_row(pcfg: Config) -> Dict[str, Any]:
    market = pcfg.market
    return {
        "market_id": market.id,
        "market_label": market.label,
        "region": market.legacy_region,
        "currency": market.currency,
        "badge_class": market.display.badge_class,
        "exchange_label": market.display.exchange_label,
        "broker_label": market.display.broker_label,
    }
