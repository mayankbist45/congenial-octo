"""Classify stocks as growth/value for intraday momentum vs mean-reversion bias."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, Optional

from src.config import Config
from src.models import FundamentalSnapshot


@dataclass(frozen=True)
class StyleProfile:
    symbol: str
    label: str  # growth | value | neutral | value_trap | unknown
    intraday_bias: str  # momentum | mean_reversion | neutral
    pe_ratio: Optional[float]
    peg_ratio: Optional[float]
    earnings_growth_pct: Optional[float]
    revenue_growth_pct: Optional[float]
    rationale: str

    def summary(self) -> str:
        pe = f"{self.pe_ratio:.1f}" if self.pe_ratio is not None else "n/a"
        peg = f"{self.peg_ratio:.2f}" if self.peg_ratio is not None else "n/a"
        eg = (
            f"{self.earnings_growth_pct:+.1f}%"
            if self.earnings_growth_pct is not None
            else "n/a"
        )
        return (
            f"style={self.label} bias={self.intraday_bias} "
            f"P/E={pe} PEG={peg} EPS gr={eg} ({self.rationale})"
        )


def is_calan_persona(persona_id: str) -> bool:
    return persona_id == "calan" or persona_id.startswith("calan_")


def fundamentals_enabled(config: Config) -> bool:
    block = config.get("intraday.fundamentals", {}) or {}
    return bool(block.get("enabled", False))


def _fundamentals_cfg(config: Config) -> dict:
    return dict(config.get("intraday.fundamentals", {}) or {})


def _load_snapshot(config: Config, symbol: str) -> Optional[FundamentalSnapshot]:
    cfg = _fundamentals_cfg(config)
    cache_only = bool(cfg.get("cache_only", True))
    from src.fundamentals.service import FundamentalsService

    service = FundamentalsService(config)
    try:
        if cache_only:
            service._load_cache()
            return service._from_cache(symbol.upper())
        return service.get(symbol.upper(), force_refresh=False)
    finally:
        service.close()


def classify_snapshot(
    snapshot: Optional[FundamentalSnapshot], config: Config, symbol: str
) -> StyleProfile:
    if snapshot is None:
        return StyleProfile(
            symbol=symbol.upper(),
            label="unknown",
            intraday_bias="neutral",
            pe_ratio=None,
            peg_ratio=None,
            earnings_growth_pct=None,
            revenue_growth_pct=None,
            rationale="no fundamental data in cache",
        )

    cfg = _fundamentals_cfg(config)
    pe = snapshot.pe_ratio
    peg = snapshot.peg_ratio
    eg = snapshot.earnings_growth_pct
    rg = snapshot.revenue_growth_pct
    growth = eg if eg is not None else rg

    high_pe = float(cfg.get("high_pe_threshold", 35) or 35)
    value_pe = float(cfg.get("value_pe_threshold", 18) or 18)
    min_growth = float(cfg.get("min_growth_pct", 8) or 8)
    max_peg_growth = float(cfg.get("max_peg_growth", 2.5) or 2.5)

    if pe is not None and pe <= 0:
        return StyleProfile(
            symbol=symbol.upper(),
            label="value_trap",
            intraday_bias="mean_reversion",
            pe_ratio=pe,
            peg_ratio=peg,
            earnings_growth_pct=eg,
            revenue_growth_pct=rg,
            rationale="negative or zero P/E",
        )

    if pe is not None and pe >= high_pe and growth is not None and growth < 0:
        return StyleProfile(
            symbol=symbol.upper(),
            label="value_trap",
            intraday_bias="mean_reversion",
            pe_ratio=pe,
            peg_ratio=peg,
            earnings_growth_pct=eg,
            revenue_growth_pct=rg,
            rationale="high P/E with shrinking earnings",
        )

    if growth is not None and growth >= min_growth:
        if peg is not None and 0 < peg <= max_peg_growth:
            return StyleProfile(
                symbol=symbol.upper(),
                label="growth",
                intraday_bias="momentum",
                pe_ratio=pe,
                peg_ratio=peg,
                earnings_growth_pct=eg,
                revenue_growth_pct=rg,
                rationale="strong growth with reasonable PEG",
            )
        if pe is not None and pe >= value_pe:
            return StyleProfile(
                symbol=symbol.upper(),
                label="growth",
                intraday_bias="momentum",
                pe_ratio=pe,
                peg_ratio=peg,
                earnings_growth_pct=eg,
                revenue_growth_pct=rg,
                rationale="high growth supports momentum",
            )

    if pe is not None and pe <= value_pe and (growth is None or growth >= 0):
        return StyleProfile(
            symbol=symbol.upper(),
            label="value",
            intraday_bias="mean_reversion",
            pe_ratio=pe,
            peg_ratio=peg,
            earnings_growth_pct=eg,
            revenue_growth_pct=rg,
            rationale="low P/E; intraday fades more natural than breakouts",
        )

    return StyleProfile(
        symbol=symbol.upper(),
        label="neutral",
        intraday_bias="neutral",
        pe_ratio=pe,
        peg_ratio=peg,
        earnings_growth_pct=eg,
        revenue_growth_pct=rg,
        rationale="mixed valuation/growth profile",
    )


def get_style_profile(config: Config, symbol: str) -> Optional[StyleProfile]:
    if not fundamentals_enabled(config):
        return None
    snapshot = _load_snapshot(config, symbol.upper())
    return classify_snapshot(snapshot, config, symbol.upper())


def load_style_profiles(
    config: Config, symbols: Iterable[str]
) -> Dict[str, StyleProfile]:
    if not fundamentals_enabled(config):
        return {}
    out: Dict[str, StyleProfile] = {}
    for symbol in symbols:
        sym = str(symbol).upper()
        profile = get_style_profile(config, sym)
        if profile is not None:
            out[sym] = profile
    return out
