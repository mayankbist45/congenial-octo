"""Market registry — first-class marketplace definitions."""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from src.config import PROJECT_ROOT

MARKETS_DIR = PROJECT_ROOT / "config" / "markets"

# Legacy persona id -> market id (backward compatibility during migration).
PERSONA_MARKET_ALIASES: Dict[str, str] = {
    "balanced_us": "us_equity",
    "steady_compounder_us": "us_equity",
    "momentum_rider_us": "us_equity",
    "calan_us": "us_equity",
    "mayank_us": "us_equity",
    "revert_us": "us_equity",
    "flux_us": "us_equity",
    "spread_us": "us_equity",
    "slice_us": "us_equity",
    "xstat_us": "us_equity",
}

LEGACY_REGION_TO_MARKET: Dict[str, str] = {
    "IN": "india_equity",
    "US": "us_equity",
}


@dataclass(frozen=True)
class SessionSpec:
    open: str
    close: str


@dataclass(frozen=True)
class IntradayWatchSchedule:
    personas: List[str]
    start_offset_min: int = -5
    stop_offset_min: int = 5


@dataclass(frozen=True)
class MarketSchedule:
    swing_cycle: str
    swing_close: str
    intraday_watch: Optional[IntradayWatchSchedule] = None


@dataclass(frozen=True)
class MarketDisplay:
    badge_class: str
    broker_label: str
    exchange_label: str


@dataclass(frozen=True)
class Market:
    """Static definition for a tradable marketplace."""

    id: str
    label: str
    legacy_region: str
    asset_class: str
    exchange: str
    timezone: str
    currency: str
    currency_symbol: str
    session: SessionSpec
    broker_provider: str
    broker_product_types: List[str]
    data: Dict[str, Any]
    costs: str
    prompts: str
    fund: Dict[str, Any]
    universe: Dict[str, Any]
    execution: Dict[str, Any]
    scheduling: MarketSchedule
    display: MarketDisplay
    raw: Dict[str, Any] = field(repr=False, default_factory=dict)

    @property
    def tz_abbrev(self) -> str:
        if self.legacy_region == "US":
            return "ET"
        if self.legacy_region == "IN":
            return "IST"
        return self.timezone.split("/")[-1].upper()


def _parse_time(value: str) -> str:
    return str(value).strip()


def _load_market_yaml(path: Path) -> Market:
    with open(path, "r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}

    session_raw = raw.get("session", {}) or {}
    regular = session_raw.get("regular", session_raw) or {}
    broker = raw.get("broker", {}) or {}
    scheduling_raw = raw.get("scheduling", {}) or {}
    intraday_raw = scheduling_raw.get("intraday_watch")
    intraday: Optional[IntradayWatchSchedule] = None
    if isinstance(intraday_raw, dict):
        intraday = IntradayWatchSchedule(
            personas=[str(p) for p in intraday_raw.get("personas", [])],
            start_offset_min=int(intraday_raw.get("start_offset_min", -5)),
            stop_offset_min=int(intraday_raw.get("stop_offset_min", 5)),
        )
    display_raw = raw.get("display", {}) or {}

    return Market(
        id=str(raw["id"]),
        label=str(raw.get("label", raw["id"])),
        legacy_region=str(raw.get("legacy_region", "IN")).upper(),
        asset_class=str(raw.get("asset_class", "equity")),
        exchange=str(raw.get("exchange", "")),
        timezone=str(raw.get("timezone", "Asia/Kolkata")),
        currency=str(raw.get("currency", "INR")).upper(),
        currency_symbol=str(raw.get("currency_symbol", "")),
        session=SessionSpec(
            open=_parse_time(regular.get("open", "09:15")),
            close=_parse_time(regular.get("close", "15:30")),
        ),
        broker_provider=str(broker.get("provider", "angelone")).lower(),
        broker_product_types=[str(p) for p in broker.get("product_types", ["CASH"])],
        data=dict(raw.get("data", {}) or {}),
        costs=str(raw.get("costs", "")),
        prompts=str(raw.get("prompts", "")),
        fund=dict(raw.get("fund", {}) or {}),
        universe=dict(raw.get("universe", {}) or {}),
        execution=dict(raw.get("execution", {}) or {}),
        scheduling=MarketSchedule(
            swing_cycle=_parse_time(scheduling_raw.get("swing_cycle", "08:15")),
            swing_close=_parse_time(scheduling_raw.get("swing_close", "18:00")),
            intraday_watch=intraday,
        ),
        display=MarketDisplay(
            badge_class=str(display_raw.get("badge_class", "region-in")),
            broker_label=str(display_raw.get("broker_label", "")),
            exchange_label=str(display_raw.get("exchange_label", "")),
        ),
        raw=raw,
    )


@lru_cache(maxsize=1)
def load_markets() -> Dict[str, Market]:
    markets: Dict[str, Market] = {}
    if not MARKETS_DIR.exists():
        return markets
    for path in sorted(MARKETS_DIR.glob("*.yaml")):
        if path.name.startswith("._") or path.name.startswith("."):
            continue
        market = _load_market_yaml(path)
        markets[market.id] = market
    return markets


def get_market(market_id: str) -> Market:
    markets = load_markets()
    key = (market_id or "india_equity").strip()
    market = markets.get(key)
    if market is None:
        available = ", ".join(sorted(markets)) or "(none)"
        raise ValueError(f"Unknown market {key!r}; available: {available}")
    return market


def get_market_by_region(region: str) -> Market:
    market_id = LEGACY_REGION_TO_MARKET.get((region or "IN").upper(), "india_equity")
    return get_market(market_id)


def list_markets() -> List[Market]:
    return list(load_markets().values())


def resolve_market_id(
    persona_id: str,
    persona_def: Optional[Dict[str, Any]] = None,
    legacy_region: Optional[str] = None,
) -> str:
    """Resolve market id from persona config, legacy region, or persona id."""
    persona_def = persona_def or {}
    explicit = persona_def.get("market")
    if explicit:
        return str(explicit)

    if persona_id in PERSONA_MARKET_ALIASES:
        return PERSONA_MARKET_ALIASES[persona_id]

    connectors = persona_def.get("connectors") or {}
    if isinstance(connectors, dict) and connectors.get("market_region"):
        region = str(connectors["market_region"]).upper()
        return LEGACY_REGION_TO_MARKET.get(region, "india_equity")

    if legacy_region:
        return LEGACY_REGION_TO_MARKET.get(legacy_region.upper(), "india_equity")

    return "india_equity"


def persona_strategy_type(persona_id: str) -> str:
    """Base strategy name without market suffix (calan_us -> calan)."""
    if persona_id.endswith("_us"):
        return persona_id[:-3]
    return persona_id
