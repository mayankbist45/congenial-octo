"""Build structured briefings for intraday LLM agents."""

from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional

import pandas as pd

from src.config import Config
from src.intraday.fundamental_style import get_style_profile, load_style_profiles
from src.intraday.models import IntradaySignal, RankedStock
from src.intraday.ofi import ofi_divergence, ofi_z_proxy


def build_catalyst_briefing(
    config: Config,
    ranked: List[RankedStock],
    frames: Dict[str, pd.DataFrame],
    as_of: datetime,
    daily_pnl: float = 0.0,
) -> str:
    persona_id = config.persona_id
    if persona_id == "revert":
        return _build_revert_regime_briefing(config, ranked, frames, as_of, daily_pnl)
    if persona_id == "flux":
        return _build_flux_flow_briefing(config, ranked, frames, as_of, daily_pnl)
    if persona_id == "spread":
        return _build_spread_analyst_briefing(config, ranked, frames, as_of, daily_pnl)
    if persona_id == "slice":
        return _build_slice_monitor_briefing(config, ranked, frames, as_of, daily_pnl)
    return _build_calan_catalyst_briefing(config, ranked, frames, as_of, daily_pnl)


def build_trade_gate_briefing(
    config: Config,
    signal: IntradaySignal,
    ranked: RankedStock,
    frame: pd.DataFrame,
    as_of: datetime,
    daily_pnl: float,
    watchlist_entry: Optional[Dict[str, object]] = None,
    estimated_costs: float = 0.0,
) -> str:
    persona_id = config.persona_id
    if persona_id == "revert":
        return _build_revert_gate_briefing(
            config, signal, ranked, frame, as_of, daily_pnl, watchlist_entry, estimated_costs
        )
    if persona_id == "flux":
        return _build_flux_gate_briefing(
            config, signal, ranked, frame, as_of, daily_pnl, watchlist_entry, estimated_costs
        )
    if persona_id == "spread":
        return _build_spread_gate_briefing(
            config, signal, ranked, frame, as_of, daily_pnl, watchlist_entry, estimated_costs
        )
    if persona_id == "slice":
        return _build_slice_gate_briefing(
            config, signal, ranked, frame, as_of, daily_pnl, watchlist_entry, estimated_costs
        )
    return _build_calan_gate_briefing(
        config, signal, ranked, frame, as_of, daily_pnl, watchlist_entry, estimated_costs
    )


def _vwap_side(frame: pd.DataFrame) -> tuple[float, str]:
    typical = (frame["high"] + frame["low"] + frame["close"]) / 3.0
    vol = frame["volume"].clip(lower=0).sum()
    if vol <= 0:
        return 0.0, ""
    vwap = float((typical * frame["volume"]).sum() / vol)
    last = float(frame["close"].iloc[-1])
    side = "above_vwap" if last >= vwap else "below_vwap"
    dist_pct = (last / vwap - 1.0) * 100.0 if vwap else 0.0
    return dist_pct, side


def _build_calan_catalyst_briefing(
    config: Config,
    ranked: List[RankedStock],
    frames: Dict[str, pd.DataFrame],
    as_of: datetime,
    daily_pnl: float,
) -> str:
    lines = [
        f"INTRADAY CATALYST SCAN — {as_of.strftime('%Y-%m-%d %H:%M')} IST",
        f"Persona: {config.persona_id}",
        f"Capital: Rs {config.secrets.initial_capital:,.0f}",
        f"Daily P&L so far: Rs {daily_pnl:,.2f}",
        "",
        "CANDIDATES (ranked by quant score):",
    ]
    styles = load_style_profiles(config, [item.symbol for item in ranked[:15]])
    for item in ranked[:15]:
        frame = frames.get(item.symbol)
        vwap_side = ""
        if frame is not None and not frame.empty:
            _, vwap_side = _vwap_side(frame)
        style_line = ""
        profile = styles.get(item.symbol.upper())
        if profile is not None:
            style_line = f"  {profile.summary()}"
        lines.append(
            f"  {item.symbol}: score={item.score:.1f} in_play={item.in_play} "
            f"gap={item.gap_pct if item.gap_pct is not None else 'n/a'}% "
            f"rvol={item.volume_expansion:.2f}x range={item.range_pct:.2f}% "
            f"trend={item.trend_cleanliness:.2f} turnover=Rs {item.turnover:,.0f} "
            f"{vwap_side}"
        )
        if style_line:
            lines.append(f"    fundamentals: {style_line.strip()}")
        lines.append(f"    reasons: {'; '.join(item.reasons)}")
    lines.append("")
    lines.append(
        "Fundamental style: growth names suit momentum (ORB/VWAP pullback); "
        "value/trap names suit mean reversion (revert persona). "
        "Identify Stocks in Play with a catalyst. Return JSON watchlist (0-5 names)."
    )
    return "\n".join(lines)


def _build_revert_regime_briefing(
    config: Config,
    ranked: List[RankedStock],
    frames: Dict[str, pd.DataFrame],
    as_of: datetime,
    daily_pnl: float,
) -> str:
    avg_trend = sum(r.trend_cleanliness for r in ranked[:8]) / max(len(ranked[:8]), 1)
    lines = [
        f"REVERT REGIME SCAN — {as_of.strftime('%Y-%m-%d %H:%M')} IST",
        f"Persona: {config.persona_id}",
        f"Capital: Rs {config.secrets.initial_capital:,.0f}",
        f"Daily P&L so far: Rs {daily_pnl:,.2f}",
        f"Avg trend cleanliness (top 8): {avg_trend:.2f}",
        "",
        "FADE CANDIDATES (ranked by reversion score):",
    ]
    for item in ranked[:15]:
        frame = frames.get(item.symbol)
        vwap_dist = "n/a"
        if frame is not None and not frame.empty:
            vwap_dist = f"{_vwap_side(frame)[0]:+.2f}%"
        lines.append(
            f"  {item.symbol}: score={item.score:.1f} gap={item.gap_pct if item.gap_pct is not None else 'n/a'}% "
            f"vwap_dist={vwap_dist} trend={item.trend_cleanliness:.2f} "
            f"turnover=Rs {item.turnover:,.0f} in_play={item.in_play}"
        )
    lines.append("")
    lines.append(
        "Classify session regime and return fade watchlist (0-5) on range/chop only."
    )
    return "\n".join(lines)


def _build_flux_flow_briefing(
    config: Config,
    ranked: List[RankedStock],
    frames: Dict[str, pd.DataFrame],
    as_of: datetime,
    daily_pnl: float,
) -> str:
    lines = [
        f"FLUX FLOW SCAN — {as_of.strftime('%Y-%m-%d %H:%M')} IST",
        f"Persona: {config.persona_id}",
        f"Capital: Rs {config.secrets.initial_capital:,.0f}",
        f"Daily P&L so far: Rs {daily_pnl:,.2f}",
        "",
        "FLOW CANDIDATES (ranked by |OFI z| × turnover):",
    ]
    for item in ranked[:10]:
        frame = frames.get(item.symbol)
        ofi_z = "n/a"
        divergence = "n/a"
        if frame is not None and len(frame) >= 6:
            ofi_z = f"{ofi_z_proxy(frame):+.2f}"
            divergence = str(ofi_divergence(frame))
        lines.append(
            f"  {item.symbol}: score={item.score:.1f} ofi_z={ofi_z} "
            f"divergence={divergence} range={item.range_pct:.2f}% "
            f"turnover=Rs {item.turnover:,.0f}"
        )
    lines.append("")
    lines.append("Classify flow quality. Return JSON watchlist (0-3 informed/exhausted).")
    return "\n".join(lines)


def _build_spread_analyst_briefing(
    config: Config,
    ranked: List[RankedStock],
    frames: Dict[str, pd.DataFrame],
    as_of: datetime,
    daily_pnl: float,
) -> str:
    pairs = config.get("intraday.pairs") or []
    lines = [
        f"SPREAD ANALYST — {as_of.strftime('%Y-%m-%d %H:%M')} IST",
        f"Persona: {config.persona_id}",
        f"Capital: Rs {config.secrets.initial_capital:,.0f}",
        f"Daily P&L so far: Rs {daily_pnl:,.2f}",
        "",
        "CONFIGURED PAIRS:",
    ]
    for pair in pairs:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            continue
        leg_a, leg_b = str(pair[0]).upper(), str(pair[1]).upper()
        fa = frames.get(leg_a)
        fb = frames.get(leg_b)
        spread_z = "n/a"
        if fa is not None and fb is not None and not fa.empty and not fb.empty:
            spread_z = f"{_spread_z_proxy(fa, fb):+.2f}"
        lines.append(f"  {leg_a}/{leg_b}: spread_z={spread_z}")
    lines.append("")
    lines.append("Return pair_opportunities JSON (0-2 technical divergences).")
    return "\n".join(lines)


def _build_slice_monitor_briefing(
    config: Config,
    ranked: List[RankedStock],
    frames: Dict[str, pd.DataFrame],
    as_of: datetime,
    daily_pnl: float,
) -> str:
    parent_orders = config.get("intraday.parent_orders") or []
    lines = [
        f"SLICE MONITOR — {as_of.strftime('%Y-%m-%d %H:%M')} IST",
        f"Persona: {config.persona_id}",
        f"Capital: Rs {config.secrets.initial_capital:,.0f}",
        f"Daily P&L so far: Rs {daily_pnl:,.2f}",
        "",
        "PARENT ORDERS:",
    ]
    if not parent_orders:
        lines.append("  (none configured)")
    for order in parent_orders:
        if not isinstance(order, dict):
            continue
        symbol = str(order.get("symbol", "")).upper()
        lines.append(
            f"  {symbol}: {order.get('side', 'BUY')} qty={order.get('qty', 0)} "
            f"schedule={order.get('schedule', 'twap')} "
            f"{order.get('start', '10:00')}-{order.get('end', '14:00')}"
        )
    lines.append("")
    lines.append("Report implementation shortfall per schedule. Return JSON schedules.")
    return "\n".join(lines)


def _build_calan_gate_briefing(
    config: Config,
    signal: IntradaySignal,
    ranked: RankedStock,
    frame: pd.DataFrame,
    as_of: datetime,
    daily_pnl: float,
    watchlist_entry: Optional[Dict[str, object]],
    estimated_costs: float,
) -> str:
    reward = abs(signal.target_price - signal.entry_price)
    risk = signal.risk_per_share
    rr = reward / risk if risk > 0 else 0.0
    typical = (frame["high"] + frame["low"] + frame["close"]) / 3.0
    vol = frame["volume"].clip(lower=0).sum()
    vwap = float((typical * frame["volume"]).sum() / vol) if vol > 0 else signal.entry_price
    last = float(frame["close"].iloc[-1])
    catalyst_conf = 0.0
    catalyst_class = "none"
    if watchlist_entry:
        catalyst_conf = float(watchlist_entry.get("confidence", 0.0) or 0.0)
        catalyst_class = str(watchlist_entry.get("catalyst_class", "none"))

    style_line = ""
    profile = get_style_profile(config, signal.symbol)
    if profile is not None:
        style_line = profile.summary()

    lines = [
        f"TRADE GATE — {as_of.strftime('%Y-%m-%d %H:%M')} IST",
        f"Symbol: {signal.symbol}",
        f"Strategy: {signal.strategy}",
        f"Side: {signal.side}",
        f"Entry: {signal.entry_price:.2f}  Stop: {signal.stop_price:.2f}  "
        f"Target: {signal.target_price:.2f}",
        f"R:R gross: {rr:.2f}",
        f"Estimated round-trip costs: Rs {estimated_costs:,.2f}",
        f"VWAP: {vwap:.2f}  Last: {last:.2f}",
        f"Rank score: {ranked.score:.1f}  in_play: {ranked.in_play}  "
        f"trend: {ranked.trend_cleanliness:.2f}",
        f"Catalyst: {catalyst_class}  confidence: {catalyst_conf:.2f}",
    ]
    if style_line:
        lines.append(f"Fundamentals: {style_line}")
    lines.extend(
        [
            f"Daily P&L: Rs {daily_pnl:,.2f}  "
            f"max daily loss: {config.get('intraday.max_daily_loss_pct', 1.5)}%",
            f"Reason: {signal.reason}",
            "",
            "Approve or veto this trade. Return JSON only.",
        ]
    )
    return "\n".join(lines)


def _build_revert_gate_briefing(
    config: Config,
    signal: IntradaySignal,
    ranked: RankedStock,
    frame: pd.DataFrame,
    as_of: datetime,
    daily_pnl: float,
    watchlist_entry: Optional[Dict[str, object]],
    estimated_costs: float,
) -> str:
    base = _build_calan_gate_briefing(
        config, signal, ranked, frame, as_of, daily_pnl, watchlist_entry, estimated_costs
    )
    regime = "unknown"
    if watchlist_entry:
        regime = str(watchlist_entry.get("session_regime", "unknown"))
    return base.replace(
        "TRADE GATE",
        f"REVERSION GATE (session_regime={regime})",
        1,
    )


def _build_flux_gate_briefing(
    config: Config,
    signal: IntradaySignal,
    ranked: RankedStock,
    frame: pd.DataFrame,
    as_of: datetime,
    daily_pnl: float,
    watchlist_entry: Optional[Dict[str, object]],
    estimated_costs: float,
) -> str:
    reward = abs(signal.target_price - signal.entry_price)
    risk = signal.risk_per_share
    rr = reward / risk if risk > 0 else 0.0
    flow_quality = "none"
    ofi_z = 0.0
    if watchlist_entry:
        flow_quality = str(watchlist_entry.get("flow_quality", "none"))
        ofi_z = float(watchlist_entry.get("ofi_z", 0.0) or 0.0)
    divergence = ofi_divergence(frame)
    atr_pct = ranked.range_pct

    return "\n".join(
        [
            f"TOXICITY GATE — {as_of.strftime('%Y-%m-%d %H:%M')} IST",
            f"Symbol: {signal.symbol}",
            f"Strategy: {signal.strategy}",
            f"Side: {signal.side}",
            f"Entry: {signal.entry_price:.2f}  Stop: {signal.stop_price:.2f}  "
            f"Target: {signal.target_price:.2f}",
            f"R:R gross: {rr:.2f}",
            f"Estimated round-trip costs: Rs {estimated_costs:,.2f}",
            f"flow_quality: {flow_quality}  ofi_z: {ofi_z:+.2f}  divergence: {divergence}",
            f"ATR pct: {atr_pct:.2f}%",
            f"Daily P&L: Rs {daily_pnl:,.2f}  "
            f"max daily loss: {config.get('intraday.max_daily_loss_pct', 0.8)}%",
            f"Trade window end: {config.get('intraday.trade_window_end', '14:30')}",
            f"Reason: {signal.reason}",
            "",
            "Approve or veto this flow trade. Return JSON only.",
        ]
    )


def _build_spread_gate_briefing(
    config: Config,
    signal: IntradaySignal,
    ranked: RankedStock,
    frame: pd.DataFrame,
    as_of: datetime,
    daily_pnl: float,
    watchlist_entry: Optional[Dict[str, object]],
    estimated_costs: float,
) -> str:
    pair_id = str(watchlist_entry.get("pair_id", "n/a")) if watchlist_entry else "n/a"
    return "\n".join(
        [
            f"CONVERGENCE GATE — {as_of.strftime('%Y-%m-%d %H:%M')} IST",
            f"Primary leg: {signal.symbol}",
            f"Pair: {pair_id}",
            f"Strategy: {signal.strategy}",
            f"Side: {signal.side}",
            f"Entry: {signal.entry_price:.2f}",
            f"Estimated round-trip costs (both legs): Rs {estimated_costs:,.2f}",
            f"Daily P&L: Rs {daily_pnl:,.2f}",
            f"Reason: {signal.reason}",
            "",
            "Approve or veto this pair trade. Return JSON only.",
        ]
    )


def _build_slice_gate_briefing(
    config: Config,
    signal: IntradaySignal,
    ranked: RankedStock,
    frame: pd.DataFrame,
    as_of: datetime,
    daily_pnl: float,
    watchlist_entry: Optional[Dict[str, object]],
    estimated_costs: float,
) -> str:
    return "\n".join(
        [
            f"SLICE GATE — {as_of.strftime('%Y-%m-%d %H:%M')} IST",
            f"Symbol: {signal.symbol}",
            f"Strategy: {signal.strategy}",
            f"Side: {signal.side}",
            f"Entry: {signal.entry_price:.2f}  qty slice (predetermined)",
            f"Daily P&L: Rs {daily_pnl:,.2f}",
            "",
            "Approve predetermined slice unless hard limit breached. Return JSON only.",
        ]
    )


def _spread_z_proxy(fa: pd.DataFrame, fb: pd.DataFrame) -> float:
    import numpy as np

    n = min(len(fa), len(fb))
    if n < 30:
        return 0.0
    a = fa["close"].tail(n).astype(float).values
    b = fb["close"].tail(n).astype(float).values
    with np.errstate(divide="ignore", invalid="ignore"):
        spread = np.log(a / b)
    spread = spread[np.isfinite(spread)]
    if len(spread) < 30:
        return 0.0
    mu = float(spread.mean())
    sigma = float(spread.std())
    if sigma <= 1e-9:
        return 0.0
    return float((spread[-1] - mu) / sigma)
