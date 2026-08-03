"""Prompts for intraday LLM agents, routed by persona ID."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

# --- calan (momentum / Stocks in Play) ---

CATALYST_SYSTEM_PROMPT = """You are the intraday desk analyst for an Indian equity day-trading book (NSE,
Nifty 50 liquid names). Your job is NOT to pick trades. You identify "Stocks in
Play" — names with abnormal activity AND a plausible catalyst — following the
Stocks-in-Play filter (Zarattini, Barbon & Aziz, 2024).

RULES
- Use ONLY the data in the briefing.
- A name is in_play only if BOTH abnormal activity AND a classifiable catalyst
  (or a clean technical continuation with extreme volume).
- Catalyst classes: earnings_results, sector_news, company_news, index_beta,
  technical_only, none.
- technical_only is valid ONLY if gap+volume are extreme AND trend cleanliness
  >= 0.6.
- Prefer 0–5 names. Empty watchlist is valid.
- Do not recommend entries before 09:45 IST.

OUTPUT: single JSON object only:
{
  "session_view": "2 sentences on session tone",
  "trade_day_quality": "poor" | "fair" | "good",
  "watchlist": [
    {
      "symbol": "NSE symbol",
      "in_play": true,
      "catalyst_class": "earnings_results" | "sector_news" | "company_news" | "index_beta" | "technical_only",
      "direction_bias": "long" | "short" | "either",
      "confidence": 0.0-1.0,
      "rationale": "1-2 sentences from briefing data only"
    }
  ]
}"""

TRADE_GATE_SYSTEM_PROMPT = """You are the intraday risk officer for a paper-trading NSE book (MIS, flat by
15:15). The rule engine proposed ONE trade. Approve or veto.

VETO if:
- strategy is scalping
- time >= 14:30 IST
- daily loss >= 1.0% of capital
- catalyst confidence < 0.55 (from watchlist)
- reward:risk after estimated costs < 1.2 for ORB or < 1.5 for VWAP
- stop distance < 0.15% of entry (too tight for NSE noise)

APPROVE ORB only if breakout has volume confirmation and price on correct side
of VWAP for trade direction.

OUTPUT JSON only:
{
  "decision": "approve" | "veto",
  "symbol": "...",
  "strategy": "opening_range_breakout" | "vwap_pullback",
  "side": "BUY" | "SELL",
  "adjusted_stop": null or number,
  "adjusted_target": null or number,
  "use_trailing_stop": true | false,
  "trailing_stop_pct": null or number,
  "confidence": 0.0-1.0,
  "rationale": "2-3 sentences"
}"""

# --- revert (mean reversion) ---

REVERT_REGIME_PROMPT = """You are the regime analyst for an intraday mean-reversion book on NSE Nifty 50.
Your job is NOT to pick trades. Classify the session and identify fade candidates
only on range-bound or choppy days.

RULES
- Use ONLY the data in the briefing.
- session_regime "trend" → empty watchlist (no fades on trend days).
- Only "range" or "chop" regimes produce fade candidates.
- Prefer names far from VWAP with LOW trend cleanliness and small gaps.
- Prefer 0–5 names. Empty watchlist is valid.
- Do not recommend entries before 10:00 IST.

OUTPUT: single JSON object only:
{
  "session_regime": "trend" | "range" | "chop",
  "session_view": "2 sentences on session tone",
  "trade_day_quality": "poor" | "fair" | "good",
  "watchlist": [
    {
      "symbol": "NSE symbol",
      "fade_candidate": true,
      "direction_bias": "long" | "short" | "either",
      "confidence": 0.0-1.0,
      "rationale": "1-2 sentences from briefing data only"
    }
  ]
}"""

REVERT_GATE_PROMPT = """You are the reversion risk officer for a paper-trading NSE mean-reversion book
(MIS, flat by 15:15). The rule engine proposed ONE fade trade. Approve or veto.

VETO if:
- session_regime is "trend"
- time >= 14:00 IST
- daily loss >= max daily loss from briefing
- trend cleanliness at signal > 0.45
- fade confidence < 0.55 (from watchlist)
- reward:risk after estimated costs < 1.0
- stop distance < 0.12% of entry

OUTPUT JSON only:
{
  "decision": "approve" | "veto",
  "symbol": "...",
  "strategy": "vwap_fade" | "opening_gap_fade" | "range_revert",
  "side": "BUY" | "SELL",
  "adjusted_stop": null or number,
  "adjusted_target": null or number,
  "use_trailing_stop": true | false,
  "trailing_stop_pct": null or number,
  "confidence": 0.0-1.0,
  "rationale": "2-3 sentences"
}"""

# --- flux (order flow imbalance proxy) ---

FLUX_FLOW_PROMPT = """You are the flow analyst for an intraday order-flow book on NSE (top liquid
Nifty names). Your job is NOT to pick trades. Classify flow quality and identify
names where signed pressure (OFI proxy) is informative.

RULES
- Use ONLY the data in the briefing.
- flow_quality "informed" → persistent OFI z-score with price confirmation.
- flow_quality "exhausted" → extreme OFI but price diverges (for exhaustion fades).
- flow_quality "noise" → do NOT include on watchlist.
- Prefer 0–3 names. Empty watchlist is valid.

OUTPUT: single JSON object only:
{
  "flow_summary": "2 sentences on session flow tone",
  "trade_day_quality": "poor" | "fair" | "good",
  "watchlist": [
    {
      "symbol": "NSE symbol",
      "flow_quality": "informed" | "noise" | "exhausted",
      "direction_bias": "long" | "short" | "either",
      "ofi_z": 0.0,
      "confidence": 0.0-1.0,
      "rationale": "1-2 sentences from briefing data only"
    }
  ]
}"""

FLUX_TOXICITY_PROMPT = """You are the toxicity gate for a paper-trading NSE flow book (MIS, flat by 15:15).
The rule engine proposed ONE flow trade. Approve or veto.

VETO if:
- flow_quality is "noise" for ofi_momentum
- OFI and price diverge (adverse selection) for ofi_momentum
- ATR pct > 1.2% (spread too wide)
- daily loss >= 0.8% of capital
- time >= trade window end from briefing
- reward:risk after costs < 1.0 for ofi_momentum or < 0.8 for ofi_exhaustion
- stop distance < 0.10% of entry

OUTPUT JSON only:
{
  "decision": "approve" | "veto",
  "symbol": "...",
  "strategy": "ofi_momentum" | "ofi_exhaustion",
  "side": "BUY" | "SELL",
  "adjusted_stop": null or number,
  "adjusted_target": null or number,
  "use_trailing_stop": true | false,
  "trailing_stop_pct": null or number,
  "confidence": 0.0-1.0,
  "rationale": "2-3 sentences"
}"""

# --- spread (pairs relative value) ---

SPREAD_ANALYST_PROMPT = """You are the spread analyst for an intraday pairs mean-reversion book on NSE.
Your job is NOT to pick individual stocks. Identify pair spread opportunities
where cointegrated legs have diverged for technical (not fundamental) reasons.

RULES
- Use ONLY the data in the briefing.
- Only pairs with cointegration_ok true and divergence_type "technical".
- Veto pairs where either leg has earnings or fundamental catalyst.
- Max 2 pair opportunities. Empty list is valid.

OUTPUT: single JSON object only:
{
  "session_view": "2 sentences",
  "trade_day_quality": "poor" | "fair" | "good",
  "pair_opportunities": [
    {
      "leg_a": "SYMBOL",
      "leg_b": "SYMBOL",
      "spread_z": 0.0,
      "divergence_type": "fundamental" | "technical",
      "cointegration_ok": true,
      "direction": "short_spread" | "long_spread",
      "confidence": 0.0-1.0,
      "rationale": "1-2 sentences"
    }
  ]
}"""

SPREAD_CONVERGENCE_PROMPT = """You are the convergence gate for a paper-trading NSE pairs book (MIS, flat by
15:15). The rule engine proposed ONE pair trade (two legs). Approve or veto.

VETO if:
- divergence_type is "fundamental"
- spread still widening (> 0.5 sigma in last 3 bars per briefing)
- either leg below minimum turnover
- two-leg margin exceeds per-leg cap
- daily loss limit hit
- time >= 14:00 IST

OUTPUT JSON only:
{
  "decision": "approve" | "veto",
  "symbol": "leg_a symbol (primary)",
  "strategy": "pair_zscore",
  "side": "BUY" | "SELL",
  "adjusted_stop": null or number,
  "adjusted_target": null or number,
  "use_trailing_stop": false,
  "trailing_stop_pct": null,
  "confidence": 0.0-1.0,
  "rationale": "2-3 sentences"
}"""

# --- slice (execution monitor) ---

SLICE_MONITOR_PROMPT = """You are the execution monitor for a TWAP/VWAP scheduling book on NSE. Parent
orders are split into predetermined child slices. You do NOT approve or veto
trades — you report implementation shortfall vs arrival price and session VWAP.

RULES
- Use ONLY the data in the briefing.
- Flag if slippage exceeds threshold mid-schedule.
- No directional opinions.

OUTPUT: single JSON object only:
{
  "session_view": "2 sentences on execution quality",
  "trade_day_quality": "poor" | "fair" | "good",
  "schedules": [
    {
      "symbol": "NSE symbol",
      "schedule_type": "twap" | "vwap",
      "slices_done": 0,
      "slices_total": 0,
      "shortfall_bps": 0.0,
      "vwap_slippage_bps": 0.0,
      "on_track": true,
      "rationale": "1-2 sentences"
    }
  ]
}"""

SLICE_GATE_PROMPT = """You are the execution gate for predetermined TWAP/VWAP child slices. Approve
every valid slice unless hard limits are breached.

VETO only if:
- daily loss limit hit
- parent order cancelled
- time past schedule end

OUTPUT JSON only:
{
  "decision": "approve" | "veto",
  "symbol": "...",
  "strategy": "twap_slice" | "vwap_schedule",
  "side": "BUY" | "SELL",
  "adjusted_stop": null,
  "adjusted_target": null,
  "use_trailing_stop": false,
  "trailing_stop_pct": null,
  "confidence": 1.0,
  "rationale": "predetermined slice"
}"""

# --- shared constants ---

CATALYST_CLASSES = (
    "earnings_results",
    "sector_news",
    "company_news",
    "index_beta",
    "technical_only",
    "none",
)
TRADE_DAY_QUALITIES = ("poor", "fair", "good")
SESSION_REGIMES = ("trend", "range", "chop")
FLOW_QUALITIES = ("informed", "noise", "exhausted")
DIVERGENCE_TYPES = ("fundamental", "technical")

CALAN_GATE_STRATEGIES = ("opening_range_breakout", "vwap_pullback")
GATE_STRATEGIES = CALAN_GATE_STRATEGIES  # backward compat
REVERT_GATE_STRATEGIES = ("vwap_fade", "opening_gap_fade", "range_revert")
FLUX_GATE_STRATEGIES = ("ofi_momentum", "ofi_exhaustion")
SPREAD_GATE_STRATEGIES = ("pair_zscore",)
SLICE_GATE_STRATEGIES = ("twap_slice", "vwap_schedule")

DEFAULT_PERSONA_ID = "calan"


def _clean_watch_entry(
    item: dict,
    *,
    in_play_key: str = "in_play",
    in_play_default: bool = False,
) -> Optional[dict]:
    symbol = str(item.get("symbol", "")).upper()
    if not symbol:
        return None
    bias = item.get("direction_bias", "either")
    if bias not in ("long", "short", "either"):
        bias = "either"
    try:
        confidence = float(item.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    entry: Dict[str, Any] = {
        "symbol": symbol,
        "in_play": bool(item.get(in_play_key, in_play_default)),
        "direction_bias": bias,
        "confidence": max(0.0, min(1.0, confidence)),
        "rationale": str(item.get("rationale", "")).strip(),
    }
    for optional in (
        "catalyst_class",
        "flow_quality",
        "ofi_z",
        "fade_candidate",
        "session_regime",
    ):
        if optional in item:
            entry[optional] = item[optional]
    return entry


def validate_catalyst_payload(payload: Any) -> Optional[dict]:
    if not isinstance(payload, dict):
        return None
    quality = payload.get("trade_day_quality")
    if quality not in TRADE_DAY_QUALITIES:
        return None
    watchlist = payload.get("watchlist")
    if not isinstance(watchlist, list):
        return None
    cleaned: List[dict] = []
    for item in watchlist:
        if not isinstance(item, dict):
            continue
        row = _clean_watch_entry(item, in_play_key="in_play", in_play_default=False)
        if row is None:
            continue
        catalyst = item.get("catalyst_class", "none")
        if catalyst not in CATALYST_CLASSES:
            catalyst = "none"
        row["catalyst_class"] = catalyst
        row["in_play"] = bool(item.get("in_play", False))
        cleaned.append(row)
    return {
        "session_view": str(payload.get("session_view", "")).strip(),
        "trade_day_quality": quality,
        "watchlist": cleaned,
    }


def validate_revert_catalyst_payload(payload: Any) -> Optional[dict]:
    if not isinstance(payload, dict):
        return None
    regime = payload.get("session_regime")
    if regime not in SESSION_REGIMES:
        return None
    quality = payload.get("trade_day_quality")
    if quality not in TRADE_DAY_QUALITIES:
        return None
    watchlist = payload.get("watchlist")
    if not isinstance(watchlist, list):
        return None
    cleaned: List[dict] = []
    for item in watchlist:
        if not isinstance(item, dict):
            continue
        if regime == "trend":
            continue
        row = _clean_watch_entry(item, in_play_key="fade_candidate", in_play_default=True)
        if row is None:
            continue
        row["fade_candidate"] = bool(item.get("fade_candidate", True))
        row["in_play"] = row["fade_candidate"]
        row["session_regime"] = regime
        cleaned.append(row)
    return {
        "session_regime": regime,
        "session_view": str(payload.get("session_view", "")).strip(),
        "trade_day_quality": quality,
        "watchlist": cleaned,
    }


def validate_flux_catalyst_payload(payload: Any) -> Optional[dict]:
    if not isinstance(payload, dict):
        return None
    quality = payload.get("trade_day_quality")
    if quality not in TRADE_DAY_QUALITIES:
        return None
    watchlist = payload.get("watchlist")
    if not isinstance(watchlist, list):
        return None
    cleaned: List[dict] = []
    for item in watchlist:
        if not isinstance(item, dict):
            continue
        flow = item.get("flow_quality", "noise")
        if flow not in FLOW_QUALITIES or flow == "noise":
            continue
        row = _clean_watch_entry(item, in_play_key="flow_quality", in_play_default=False)
        if row is None:
            continue
        row["flow_quality"] = flow
        row["in_play"] = flow in ("informed", "exhausted")
        try:
            row["ofi_z"] = float(item.get("ofi_z", 0.0))
        except (TypeError, ValueError):
            row["ofi_z"] = 0.0
        cleaned.append(row)
    return {
        "flow_summary": str(payload.get("flow_summary", "")).strip(),
        "session_view": str(payload.get("flow_summary", payload.get("session_view", ""))).strip(),
        "trade_day_quality": quality,
        "watchlist": cleaned,
    }


def validate_spread_catalyst_payload(payload: Any) -> Optional[dict]:
    if not isinstance(payload, dict):
        return None
    quality = payload.get("trade_day_quality")
    if quality not in TRADE_DAY_QUALITIES:
        return None
    pairs = payload.get("pair_opportunities")
    if not isinstance(pairs, list):
        return None
    cleaned_pairs: List[dict] = []
    watchlist: List[dict] = []
    for item in pairs:
        if not isinstance(item, dict):
            continue
        leg_a = str(item.get("leg_a", "")).upper()
        leg_b = str(item.get("leg_b", "")).upper()
        if not leg_a or not leg_b:
            continue
        div = item.get("divergence_type", "fundamental")
        if div not in DIVERGENCE_TYPES:
            div = "fundamental"
        if div != "technical" or not bool(item.get("cointegration_ok", False)):
            continue
        try:
            confidence = float(item.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        try:
            spread_z = float(item.get("spread_z", 0.0))
        except (TypeError, ValueError):
            spread_z = 0.0
        direction = item.get("direction", "short_spread")
        if direction not in ("short_spread", "long_spread"):
            direction = "short_spread"
        pair_row = {
            "leg_a": leg_a,
            "leg_b": leg_b,
            "spread_z": spread_z,
            "divergence_type": div,
            "cointegration_ok": True,
            "direction": direction,
            "confidence": max(0.0, min(1.0, confidence)),
            "rationale": str(item.get("rationale", "")).strip(),
        }
        cleaned_pairs.append(pair_row)
        for leg in (leg_a, leg_b):
            watchlist.append(
                {
                    "symbol": leg,
                    "in_play": True,
                    "direction_bias": "either",
                    "confidence": pair_row["confidence"],
                    "rationale": pair_row["rationale"],
                    "pair_id": f"{leg_a}:{leg_b}",
                }
            )
    return {
        "session_view": str(payload.get("session_view", "")).strip(),
        "trade_day_quality": quality,
        "pair_opportunities": cleaned_pairs,
        "watchlist": watchlist,
    }


def validate_slice_monitor_payload(payload: Any) -> Optional[dict]:
    if not isinstance(payload, dict):
        return None
    quality = payload.get("trade_day_quality")
    if quality not in TRADE_DAY_QUALITIES:
        return None
    schedules = payload.get("schedules")
    if not isinstance(schedules, list):
        return None
    cleaned: List[dict] = []
    watchlist: List[dict] = []
    for item in schedules:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol", "")).upper()
        if not symbol:
            continue
        schedule_type = item.get("schedule_type", "twap")
        if schedule_type not in ("twap", "vwap"):
            schedule_type = "twap"
        on_track = bool(item.get("on_track", True))
        cleaned.append(
            {
                "symbol": symbol,
                "schedule_type": schedule_type,
                "slices_done": int(item.get("slices_done", 0) or 0),
                "slices_total": int(item.get("slices_total", 0) or 0),
                "shortfall_bps": float(item.get("shortfall_bps", 0.0) or 0.0),
                "vwap_slippage_bps": float(item.get("vwap_slippage_bps", 0.0) or 0.0),
                "on_track": on_track,
                "rationale": str(item.get("rationale", "")).strip(),
            }
        )
        watchlist.append(
            {
                "symbol": symbol,
                "in_play": on_track,
                "direction_bias": "either",
                "confidence": 1.0 if on_track else 0.5,
                "rationale": str(item.get("rationale", "")).strip(),
            }
        )
    return {
        "session_view": str(payload.get("session_view", "")).strip(),
        "trade_day_quality": quality,
        "schedules": cleaned,
        "watchlist": watchlist,
    }


def validate_gate_payload(
    payload: Any,
    allowed_strategies: Optional[Tuple[str, ...]] = None,
) -> Optional[dict]:
    if not isinstance(payload, dict):
        return None
    decision = payload.get("decision")
    if decision not in ("approve", "veto"):
        return None
    strategy = payload.get("strategy")
    allowed = allowed_strategies or CALAN_GATE_STRATEGIES
    if strategy not in allowed:
        return None
    side = payload.get("side")
    if side not in ("BUY", "SELL"):
        return None
    try:
        confidence = float(payload.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    return {
        "decision": decision,
        "symbol": str(payload.get("symbol", "")).upper(),
        "strategy": strategy,
        "side": side,
        "adjusted_stop": payload.get("adjusted_stop"),
        "adjusted_target": payload.get("adjusted_target"),
        "use_trailing_stop": bool(payload.get("use_trailing_stop", False)),
        "trailing_stop_pct": payload.get("trailing_stop_pct"),
        "confidence": max(0.0, min(1.0, confidence)),
        "rationale": str(payload.get("rationale", "")).strip(),
    }


@dataclass(frozen=True)
class PersonaPromptBundle:
    catalyst_prompt: str
    gate_prompt: Optional[str]
    validate_catalyst: Callable[[Any], Optional[dict]]
    validate_gate: Callable[[Any], Optional[dict]]
    gate_strategies: Tuple[str, ...]


def _gate_validator(strategies: Tuple[str, ...]) -> Callable[[Any], Optional[dict]]:
    def _validate(payload: Any) -> Optional[dict]:
        return validate_gate_payload(payload, strategies)

    return _validate


PERSONA_PROMPTS: Dict[str, PersonaPromptBundle] = {
    "calan": PersonaPromptBundle(
        catalyst_prompt=CATALYST_SYSTEM_PROMPT,
        gate_prompt=TRADE_GATE_SYSTEM_PROMPT,
        validate_catalyst=validate_catalyst_payload,
        validate_gate=_gate_validator(CALAN_GATE_STRATEGIES),
        gate_strategies=CALAN_GATE_STRATEGIES,
    ),
    "revert": PersonaPromptBundle(
        catalyst_prompt=REVERT_REGIME_PROMPT,
        gate_prompt=REVERT_GATE_PROMPT,
        validate_catalyst=validate_revert_catalyst_payload,
        validate_gate=_gate_validator(REVERT_GATE_STRATEGIES),
        gate_strategies=REVERT_GATE_STRATEGIES,
    ),
    "flux": PersonaPromptBundle(
        catalyst_prompt=FLUX_FLOW_PROMPT,
        gate_prompt=FLUX_TOXICITY_PROMPT,
        validate_catalyst=validate_flux_catalyst_payload,
        validate_gate=_gate_validator(FLUX_GATE_STRATEGIES),
        gate_strategies=FLUX_GATE_STRATEGIES,
    ),
    "spread": PersonaPromptBundle(
        catalyst_prompt=SPREAD_ANALYST_PROMPT,
        gate_prompt=SPREAD_CONVERGENCE_PROMPT,
        validate_catalyst=validate_spread_catalyst_payload,
        validate_gate=_gate_validator(SPREAD_GATE_STRATEGIES),
        gate_strategies=SPREAD_GATE_STRATEGIES,
    ),
    "slice": PersonaPromptBundle(
        catalyst_prompt=SLICE_MONITOR_PROMPT,
        gate_prompt=SLICE_GATE_PROMPT,
        validate_catalyst=validate_slice_monitor_payload,
        validate_gate=_gate_validator(SLICE_GATE_STRATEGIES),
        gate_strategies=SLICE_GATE_STRATEGIES,
    ),
}


def get_persona_prompt_bundle(persona_id: str) -> PersonaPromptBundle:
    """Return prompt bundle for persona; fall back to calan."""
    return PERSONA_PROMPTS.get(persona_id, PERSONA_PROMPTS[DEFAULT_PERSONA_ID])
