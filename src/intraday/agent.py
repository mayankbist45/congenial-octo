"""Intraday LLM agents with rules-only fallback."""

from __future__ import annotations

from datetime import datetime, time
from typing import Dict, List, Optional

import pandas as pd

from src.config import Config, get_config
from src.intraday.briefing import build_catalyst_briefing, build_trade_gate_briefing
from src.intraday.fundamental_style import get_style_profile
from src.intraday.models import IntradaySignal, RankedStock
from src.intraday.ranking import IntradayRanker
from src.intraday.prompts import get_persona_prompt_bundle
from src.llm import LLMClient, LLMUnavailable, extract_json
from src.logging_setup import get_logger

logger = get_logger(__name__)

RETRY = "Return valid JSON only, matching the requested schema exactly."


class IntradayAgent:
    def __init__(self, config: Optional[Config] = None, llm: Optional[LLMClient] = None):
        self._config = config or get_config()
        self._llm = llm or LLMClient(self._config)

    @property
    def enabled(self) -> bool:
        return bool(self._config.get("intraday.agent.enabled", True))

    def _prompts(self):
        return get_persona_prompt_bundle(self._config.persona_id)

    def catalyst_scan(
        self,
        ranked: List[RankedStock],
        frames: Dict[str, pd.DataFrame],
        as_of: datetime,
        daily_pnl: float = 0.0,
    ) -> dict:
        use_llm = bool(self._config.get("intraday.agent.catalyst_use_llm", True))
        if use_llm and self.enabled and self._llm.available:
            result = self._catalyst_llm(ranked, frames, as_of, daily_pnl)
            if result is not None:
                result = self._filter_calan_watchlist(result, ranked)
                if result.get("watchlist"):
                    return result
        return self._catalyst_rules(ranked)

    def gate_trade(
        self,
        signal: IntradaySignal,
        ranked: RankedStock,
        frame: pd.DataFrame,
        as_of: datetime,
        daily_pnl: float,
        watchlist_entry: Optional[Dict[str, object]] = None,
        estimated_costs: float = 0.0,
    ) -> dict:
        rules = self._gate_rules(
            signal, ranked, frame, as_of, daily_pnl, watchlist_entry, estimated_costs
        )
        if rules["decision"] == "veto":
            return rules
        use_llm = self._config.get("intraday.agent.gate_use_llm", True)
        if use_llm and self.enabled and self._llm.available:
            llm = self._gate_llm(
                signal, ranked, frame, as_of, daily_pnl, watchlist_entry, estimated_costs
            )
            if llm is not None:
                if llm.get("decision") == "veto":
                    return llm
                merged = dict(rules)
                for key in ("adjusted_stop", "adjusted_target", "rationale", "confidence"):
                    if llm.get(key) is not None:
                        merged[key] = llm[key]
                if llm.get("use_trailing_stop") is not None:
                    merged["use_trailing_stop"] = llm["use_trailing_stop"]
                if llm.get("trailing_stop_pct") is not None:
                    merged["trailing_stop_pct"] = llm["trailing_stop_pct"]
                merged["decision"] = "approve"
                merged["generated_by_llm"] = True
                return merged
        return rules

    def _filter_calan_watchlist(self, payload: dict, ranked: List[RankedStock]) -> dict:
        """Drop weak LLM picks that lack gap/rvol catalyst support."""
        from src.market.registry import persona_strategy_type

        if persona_strategy_type(self._config.persona_id) != "calan":
            return payload
        ranked_map = {r.symbol.upper(): r for r in ranked}
        sip = self._config.get("intraday.stocks_in_play", {}) or {}
        min_rvol = float(sip.get("min_rvol", 2.0) or 2.0)
        min_gap = float(sip.get("min_gap_pct", 0.8) or 0.8)
        min_trend = float(sip.get("min_watchlist_trend", 0.40) or 0.40)
        kept = []
        for entry in payload.get("watchlist", []):
            symbol = str(entry.get("symbol", "")).upper()
            item = ranked_map.get(symbol)
            if item is None:
                continue
            catalyst = str(entry.get("catalyst_class", "") or "").lower()
            gap_ok = item.gap_pct is not None and abs(item.gap_pct) >= min_gap
            rvol_ok = item.volume_expansion >= min_rvol
            if gap_ok or rvol_ok:
                kept.append(entry)
                continue
            if catalyst in ("none", "index_beta") and not gap_ok and not rvol_ok:
                if item.trend_cleanliness < min_trend:
                    continue
            kept.append(entry)
        payload = dict(payload)
        payload["watchlist"] = kept
        return payload

    def _catalyst_llm(
        self,
        ranked: List[RankedStock],
        frames: Dict[str, pd.DataFrame],
        as_of: datetime,
        daily_pnl: float,
    ) -> Optional[dict]:
        briefing = build_catalyst_briefing(self._config, ranked, frames, as_of, daily_pnl)
        bundle = self._prompts()
        model = str(self._config.get("llm.digest_model", self._llm.digest_model))
        for attempt in range(2):
            user = briefing if attempt == 0 else f"{briefing}\n\n{RETRY}"
            try:
                raw = self._llm.complete(
                    bundle.catalyst_prompt, user, model=model, temperature=0.0, json_mode=True
                )
            except LLMUnavailable as exc:
                logger.warning("Intraday catalyst LLM failed: %s", exc)
                return None
            payload = bundle.validate_catalyst(extract_json(raw))
            if payload is not None:
                payload["generated_by_llm"] = True
                return payload
        return None

    def _gate_llm(
        self,
        signal: IntradaySignal,
        ranked: RankedStock,
        frame: pd.DataFrame,
        as_of: datetime,
        daily_pnl: float,
        watchlist_entry: Optional[Dict[str, object]],
        estimated_costs: float,
    ) -> Optional[dict]:
        briefing = build_trade_gate_briefing(
            self._config,
            signal,
            ranked,
            frame,
            as_of,
            daily_pnl,
            watchlist_entry,
            estimated_costs,
        )
        bundle = self._prompts()
        if bundle.gate_prompt is None:
            return None
        model = str(self._config.get("llm.digest_model", self._llm.digest_model))
        for attempt in range(2):
            user = briefing if attempt == 0 else f"{briefing}\n\n{RETRY}"
            try:
                raw = self._llm.complete(
                    bundle.gate_prompt, user, model=model, temperature=0.0, json_mode=True
                )
            except LLMUnavailable as exc:
                logger.warning("Intraday gate LLM failed for %s: %s", signal.symbol, exc)
                return None
            payload = bundle.validate_gate(extract_json(raw))
            if payload is not None:
                payload["generated_by_llm"] = True
                return payload
        return None

    def _catalyst_rules(self, ranked: List[RankedStock]) -> dict:
        persona_id = self._config.persona_id
        if persona_id == "mayank":
            from src.intraday.mayank import mayank_catalyst_rules

            return mayank_catalyst_rules(self._config, ranked)
        if persona_id == "revert":
            return self._revert_catalyst_rules(ranked)
        if persona_id == "flux":
            return self._flux_catalyst_rules(ranked)
        if persona_id == "spread":
            return self._spread_catalyst_rules(ranked)
        if persona_id == "xstat":
            return self._xstat_catalyst_rules(ranked)
        if persona_id == "slice":
            return self._slice_catalyst_rules()
        return self._calan_catalyst_rules(ranked)

    def _calan_catalyst_rules(self, ranked: List[RankedStock]) -> dict:
        min_conf = float(self._config.get("intraday.agent.min_watch_confidence", 0.55) or 0.55)
        watchlist = []
        for item in ranked:
            if not item.in_play:
                continue
            conf = min(0.95, 0.55 + item.trend_cleanliness * 0.3)
            if item.volume_expansion >= 2.0:
                conf += 0.05
            catalyst = "technical_only" if item.trend_cleanliness >= 0.6 else "index_beta"
            entry: Dict[str, object] = {
                "symbol": item.symbol,
                "in_play": True,
                "catalyst_class": catalyst,
                "direction_bias": "either",
                "confidence": round(min(conf, 0.95), 2),
                "rationale": f"Quant in_play: {'; '.join(item.reasons[:3])}",
            }
            profile = get_style_profile(self._config, item.symbol)
            if profile is not None:
                entry["fundamental_style"] = profile.label
                entry["intraday_bias"] = profile.intraday_bias
                entry["fundamental_summary"] = profile.summary()
                if profile.intraday_bias == "momentum" and profile.label == "growth":
                    entry["confidence"] = round(
                        min(float(entry["confidence"]) + 0.03, 0.95), 2
                    )
                elif profile.intraday_bias == "mean_reversion":
                    entry["confidence"] = round(
                        max(float(entry["confidence"]) - 0.05, 0.0), 2
                    )
            watchlist.append(entry)
            if len(watchlist) >= 5:
                break
        quality = "good" if len(watchlist) >= 2 else "fair" if watchlist else "poor"
        return {
            "session_view": "Rules-only catalyst scan (no LLM).",
            "trade_day_quality": quality,
            "watchlist": [w for w in watchlist if w["confidence"] >= min_conf],
            "generated_by_llm": False,
        }

    def _revert_catalyst_rules(self, ranked: List[RankedStock]) -> dict:
        avg_trend = sum(r.trend_cleanliness for r in ranked[:8]) / max(len(ranked[:8]), 1)
        if avg_trend > 0.55:
            regime = "trend"
        elif avg_trend < 0.35:
            regime = "range"
        else:
            regime = "chop"
        min_conf = float(self._config.get("intraday.agent.min_watch_confidence", 0.60) or 0.60)
        watchlist = []
        block_chop = regime == "chop" and avg_trend > 0.45
        if regime != "trend" and not block_chop:
            for item in ranked:
                vwap_dist = abs(item.vwap_distance_pct or 0.0)
                if vwap_dist < 1.0:
                    continue
                conf = min(0.95, 0.55 + vwap_dist * 0.08 + (1.0 - item.trend_cleanliness) * 0.2)
                watchlist.append(
                    {
                        "symbol": item.symbol,
                        "fade_candidate": True,
                        "in_play": True,
                        "direction_bias": "either",
                        "confidence": round(conf, 2),
                        "session_regime": regime,
                        "vwap_distance_pct": round(vwap_dist, 3),
                        "rationale": f"Rules fade: vwap_dist={vwap_dist:.2f}% trend={item.trend_cleanliness:.2f}",
                    }
                )
                if len(watchlist) >= 5:
                    break
        quality = "good" if len(watchlist) >= 2 else "fair" if watchlist else "poor"
        return {
            "session_regime": regime,
            "session_view": f"Rules-only regime scan ({regime}).",
            "trade_day_quality": quality,
            "watchlist": [w for w in watchlist if w["confidence"] >= min_conf],
            "generated_by_llm": False,
        }

    def _flux_catalyst_rules(self, ranked: List[RankedStock]) -> dict:
        min_conf = float(self._config.get("intraday.agent.min_watch_confidence", 0.50) or 0.50)
        z_entry = float(self._config.get("intraday.ofi_z_entry", 1.5) or 1.5)
        watchlist = []
        for item in ranked[:10]:
            ofi_z = float(item.ofi_z or 0.0)
            if abs(ofi_z) < z_entry * 0.85:
                continue
            if abs(ofi_z) < 1.0:
                flow_quality = "noise"
            elif "divergence=True" in " ".join(item.reasons):
                flow_quality = "exhausted"
            else:
                flow_quality = "informed"
            conf = min(0.95, 0.5 + abs(ofi_z) * 0.12)
            watchlist.append(
                {
                    "symbol": item.symbol,
                    "flow_quality": flow_quality,
                    "in_play": flow_quality != "noise",
                    "direction_bias": "long" if ofi_z > 0 else "short",
                    "ofi_z": round(ofi_z, 2),
                    "confidence": round(conf, 2),
                    "rationale": f"Rules flow: ofi_z={ofi_z:+.2f} ({flow_quality})",
                }
            )
            if len(watchlist) >= 3:
                break
        quality = "fair" if watchlist else "poor"
        return {
            "flow_summary": "Rules-only flow scan.",
            "session_view": "Rules-only flow scan.",
            "trade_day_quality": quality,
            "watchlist": [w for w in watchlist if w.get("in_play") and w["confidence"] >= min_conf],
            "generated_by_llm": False,
        }

    def _spread_catalyst_rules(self, ranked: List[RankedStock]) -> dict:
        entry_z = float(self._config.get("intraday.pair_entry_z", 1.5) or 1.5)
        opportunities = []
        watchlist = []
        seen_pairs: set[str] = set()
        for item in ranked:
            if not item.pair_id or item.spread_z is None:
                continue
            if item.pair_id in seen_pairs:
                continue
            if abs(item.spread_z) < entry_z * 0.9:
                continue
            legs = item.pair_id.split(":", 1)
            if len(legs) != 2:
                continue
            leg_a, leg_b = legs
            direction = "short_spread" if item.spread_z > 0 else "long_spread"
            conf = min(0.95, 0.5 + abs(item.spread_z) * 0.1)
            opportunities.append(
                {
                    "leg_a": leg_a,
                    "leg_b": leg_b,
                    "spread_z": item.spread_z,
                    "divergence_type": "technical",
                    "cointegration_ok": True,
                    "direction": direction,
                    "confidence": round(conf, 2),
                    "rationale": f"Rules spread z={item.spread_z:+.2f}",
                }
            )
            for leg in (leg_a, leg_b):
                watchlist.append(
                    {
                        "symbol": leg,
                        "in_play": True,
                        "direction_bias": "either",
                        "confidence": round(conf, 2),
                        "pair_id": item.pair_id,
                        "rationale": f"Pair spread z={item.spread_z:+.2f}",
                    }
                )
            seen_pairs.add(item.pair_id)
            if len(opportunities) >= 2:
                break
        if not opportunities:
            pairs = self._config.get("intraday.pairs") or []
            for pair in pairs:
                if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                    continue
                leg_a, leg_b = str(pair[0]).upper(), str(pair[1]).upper()
                pair_id = f"{leg_a}:{leg_b}"
                if pair_id in seen_pairs:
                    continue
                opportunities.append(
                    {
                        "leg_a": leg_a,
                        "leg_b": leg_b,
                        "spread_z": entry_z,
                        "divergence_type": "technical",
                        "cointegration_ok": True,
                        "direction": "short_spread",
                        "confidence": 0.55,
                        "rationale": "Rules spread fallback.",
                    }
                )
                for leg in (leg_a, leg_b):
                    watchlist.append(
                        {
                            "symbol": leg,
                            "in_play": True,
                            "direction_bias": "either",
                            "confidence": 0.55,
                            "pair_id": pair_id,
                            "rationale": "Rules spread fallback.",
                        }
                    )
                seen_pairs.add(pair_id)
                if len(opportunities) >= 2:
                    break
        return {
            "session_view": "Rules-only spread scan.",
            "trade_day_quality": "fair" if opportunities else "poor",
            "pair_opportunities": opportunities,
            "watchlist": watchlist,
            "generated_by_llm": False,
        }

    def _xstat_catalyst_rules(self, ranked: List[RankedStock]) -> dict:
        entry_z = float(self._config.get("intraday.cross_entry_z", 2.0) or 2.0)
        opportunities = []
        watchlist = []
        seen: set[str] = set()
        for item in ranked:
            if not item.pair_id or item.spread_z is None:
                continue
            if item.pair_id in seen:
                continue
            if abs(item.spread_z) < entry_z * 0.9:
                continue
            legs = item.pair_id.split(":", 1)
            if len(legs) != 2:
                continue
            anchor, leg = legs
            conf = min(0.95, 0.5 + abs(item.spread_z) * 0.1)
            opportunities.append(
                {
                    "anchor": anchor,
                    "leg": leg,
                    "residual_z": item.spread_z,
                    "confidence": round(conf, 2),
                    "rationale": f"Rules cross residual z={item.spread_z:+.2f}",
                }
            )
            for sym in (anchor, leg):
                watchlist.append(
                    {
                        "symbol": sym,
                        "in_play": True,
                        "direction_bias": "either",
                        "confidence": round(conf, 2),
                        "pair_id": item.pair_id,
                        "rationale": f"Cross basket z={item.spread_z:+.2f}",
                    }
                )
            seen.add(item.pair_id)
            if len(opportunities) >= 1:
                break
        return {
            "session_view": "Rules-only cross-stat scan.",
            "trade_day_quality": "fair" if opportunities else "poor",
            "cross_opportunities": opportunities,
            "watchlist": watchlist,
            "generated_by_llm": False,
        }

    def _slice_catalyst_rules(self) -> dict:
        orders = self._config.get("intraday.parent_orders") or []
        schedules = []
        watchlist = []
        for order in orders:
            if not isinstance(order, dict):
                continue
            symbol = str(order.get("symbol", "")).upper()
            if not symbol:
                continue
            schedules.append(
                {
                    "symbol": symbol,
                    "schedule_type": str(order.get("schedule", "twap")),
                    "slices_done": 0,
                    "slices_total": 0,
                    "shortfall_bps": 0.0,
                    "vwap_slippage_bps": 0.0,
                    "on_track": True,
                    "rationale": "Awaiting schedule start.",
                }
            )
            watchlist.append(
                {
                    "symbol": symbol,
                    "in_play": True,
                    "direction_bias": "either",
                    "confidence": 1.0,
                    "rationale": "Parent order scheduled.",
                }
            )
        return {
            "session_view": "Rules-only slice monitor.",
            "trade_day_quality": "fair" if schedules else "poor",
            "schedules": schedules,
            "watchlist": watchlist,
            "generated_by_llm": False,
        }

    def _gate_rules(
        self,
        signal: IntradaySignal,
        ranked: RankedStock,
        frame: pd.DataFrame,
        as_of: datetime,
        daily_pnl: float,
        watchlist_entry: Optional[Dict[str, object]],
        estimated_costs: float,
    ) -> dict:
        persona_id = self._config.persona_id
        if persona_id == "mayank":
            from src.intraday.mayank import REVERSION_STRATEGIES

            if signal.strategy in REVERSION_STRATEGIES:
                return self._revert_gate_rules(
                    signal, ranked, frame, as_of, daily_pnl, watchlist_entry, estimated_costs
                )
            return self._calan_gate_rules(
                signal, ranked, frame, as_of, daily_pnl, watchlist_entry, estimated_costs
            )
        if persona_id == "revert":
            return self._revert_gate_rules(
                signal, ranked, frame, as_of, daily_pnl, watchlist_entry, estimated_costs
            )
        if persona_id == "flux":
            return self._flux_gate_rules(
                signal, ranked, frame, as_of, daily_pnl, watchlist_entry, estimated_costs
            )
        if persona_id == "spread":
            return self._spread_gate_rules(
                signal, ranked, as_of, daily_pnl, watchlist_entry
            )
        if persona_id == "xstat":
            return self._xstat_gate_rules(
                signal, ranked, as_of, daily_pnl, watchlist_entry
            )
        if persona_id == "slice":
            return self._slice_gate_rules(signal, as_of, daily_pnl)
        return self._calan_gate_rules(
            signal, ranked, frame, as_of, daily_pnl, watchlist_entry, estimated_costs
        )

    def _calan_gate_rules(
        self,
        signal: IntradaySignal,
        ranked: RankedStock,
        frame: pd.DataFrame,
        as_of: datetime,
        daily_pnl: float,
        watchlist_entry: Optional[Dict[str, object]],
        estimated_costs: float,
    ) -> dict:
        if self._config.get("intraday.smoke_bypass_gate"):
            return {
                "decision": "approve",
                "symbol": signal.symbol,
                "strategy": signal.strategy,
                "side": signal.side,
                "adjusted_stop": None,
                "adjusted_target": None,
                "use_trailing_stop": False,
                "trailing_stop_pct": float(
                    self._config.get("intraday.trailing_stop_pct", 0.25) or 0.25
                ),
                "confidence": 1.0,
                "rationale": "smoke_bypass_gate (temporary test)",
                "generated_by_llm": False,
            }
        veto_reasons: List[str] = []
        sip = self._config.get("intraday.stocks_in_play", {}) or {}
        if signal.strategy == "scalping" and self._config.get(
            "intraday.agent.veto_scalping", True
        ):
            veto_reasons.append("scalping disabled")

        end_parts = str(self._config.get("intraday.trade_window_end", "14:30")).split(":")
        window_end = time(int(end_parts[0]), int(end_parts[1]))
        if as_of.time() >= window_end:
            veto_reasons.append("past trade window end")

        capital = float(self._config.secrets.initial_capital)
        max_loss = capital * float(self._config.get("intraday.max_daily_loss_pct", 1.5) or 1.5) / 100.0
        if daily_pnl <= -max_loss:
            veto_reasons.append("daily loss limit")

        style = get_style_profile(self._config, signal.symbol)
        fund_cfg = self._config.get("intraday.fundamentals", {}) or {}
        momentum_strats = {"opening_range_breakout", "vwap_pullback", "scalping"}
        if (
            style is not None
            and signal.strategy in momentum_strats
            and fund_cfg.get("enabled")
        ):
            if style.label == "value_trap" and fund_cfg.get("block_value_trap", True):
                veto_reasons.append(f"fundamental value trap ({style.rationale})")
            if (
                style.intraday_bias == "mean_reversion"
                and fund_cfg.get("block_momentum_on_value", True)
                and style.label in ("value", "value_trap")
            ):
                veto_reasons.append(
                    f"fundamental {style.label}; prefer revert persona for fades"
                )

        min_conf = float(self._config.get("intraday.agent.min_trade_confidence", 0.55) or 0.55)
        conf = self._trade_confidence(watchlist_entry, ranked, style)
        if conf < min_conf:
            veto_reasons.append("catalyst confidence too low")

        risk = signal.risk_per_share
        reward = abs(signal.target_price - signal.entry_price)
        notional = signal.entry_price * max(1, int(capital * 0.004 / max(risk, 0.01)))
        cost_pct = (estimated_costs / notional * 100.0) if notional > 0 else 0.0
        net_reward = reward - signal.entry_price * cost_pct / 100.0
        min_rr = float(
            sip.get(
                "min_rr_orb"
                if signal.strategy == "opening_range_breakout"
                else "min_rr_scalping"
                if signal.strategy == "scalping"
                else "min_rr_vwap",
                1.35
                if signal.strategy == "opening_range_breakout"
                else 1.2
                if signal.strategy == "scalping"
                else 1.5,
            )
            or (
                1.35
                if signal.strategy == "opening_range_breakout"
                else 1.2
                if signal.strategy == "scalping"
                else 1.5
            )
        )
        if risk > 0 and net_reward / risk < min_rr:
            veto_reasons.append(f"R:R after costs {net_reward / risk:.2f} < {min_rr}")

        stop_pct = risk / signal.entry_price * 100.0 if signal.entry_price > 0 else 0.0
        if signal.strategy == "scalping":
            min_stop_pct = float(
                self._config.get("intraday.scalping_stop_pct", 0.15) or 0.15
            )
        else:
            min_stop_pct = float(sip.get("min_stop_pct", 0.20) or 0.20)
        # Allow tiny float slack on stop distance (~0.005%).
        if stop_pct + 0.005 < min_stop_pct:
            veto_reasons.append(f"stop too tight ({stop_pct:.2f}% < {min_stop_pct:.2f}%)")

        if signal.strategy == "opening_range_breakout":
            bars = max(
                int(self._config.get("intraday.opening_range_minutes", 15) or 15) // 5,
                1,
            )
            session = IntradayRanker._trend_frame(
                frame, self._config, pd.Timestamp(as_of)
            )
            if len(session) >= bars:
                opening = session.iloc[:bars]
                low = float(opening["low"].min())
                high = float(opening["high"].max())
                if low > 0:
                    or_range_pct = (high / low - 1.0) * 100.0
                    min_or = float(sip.get("min_or_range_pct", 0.35) or 0.35)
                    if or_range_pct < min_or:
                        veto_reasons.append(
                            f"OR range {or_range_pct:.2f}% < {min_or:.2f}%"
                        )

        if not ranked.in_play:
            veto_reasons.append("not in_play")

        min_signal_trend = float(
            sip.get(
                "min_trend_at_signal_scalping" if signal.strategy == "scalping" else "min_trend_at_signal",
                sip.get("min_trend_at_signal", sip.get("min_trend_cleanliness", 0.5)),
            )
            or sip.get("min_trend_at_signal", sip.get("min_trend_cleanliness", 0.5))
            or 0.5
        )
        signal_trend = IntradayRanker.trend_cleanliness(
            frame, self._config, pd.Timestamp(as_of)
        )
        min_gap = float(sip.get("min_gap_pct", 0.8) or 0.8)
        gap_catalyst = ranked.gap_pct is not None and abs(ranked.gap_pct) >= min_gap
        if signal_trend < min_signal_trend and not gap_catalyst:
            veto_reasons.append(
                f"trend at signal {signal_trend:.2f} < {min_signal_trend:.2f}"
            )

        decision = "veto" if veto_reasons else "approve"
        use_trailing = decision == "approve" and signal.strategy != "scalping"
        return {
            "decision": decision,
            "symbol": signal.symbol,
            "strategy": signal.strategy,
            "side": signal.side,
            "adjusted_stop": None,
            "adjusted_target": None,
            "use_trailing_stop": use_trailing,
            "trailing_stop_pct": float(
                self._config.get("intraday.trailing_stop_pct", 0.25) or 0.25
            ),
            "confidence": conf or (0.7 if decision == "approve" else 0.3),
            "rationale": "; ".join(veto_reasons) if veto_reasons else "Rules gate passed",
            "generated_by_llm": False,
        }

    def _revert_gate_rules(
        self,
        signal: IntradaySignal,
        ranked: RankedStock,
        frame: pd.DataFrame,
        as_of: datetime,
        daily_pnl: float,
        watchlist_entry: Optional[Dict[str, object]],
        estimated_costs: float,
    ) -> dict:
        veto_reasons: List[str] = []
        regime = str(watchlist_entry.get("session_regime", "")) if watchlist_entry else ""
        vwap_dist = float(watchlist_entry.get("vwap_distance_pct", 0.0) or 0.0) if watchlist_entry else 0.0
        if regime == "trend":
            veto_reasons.append("trend regime")
        if regime == "chop" and vwap_dist < 1.5:
            veto_reasons.append("chop regime without deep extension")

        end_parts = str(self._config.get("intraday.trade_window_end", "13:00")).split(":")
        window_end = time(int(end_parts[0]), int(end_parts[1]))
        if as_of.time() >= window_end:
            veto_reasons.append("past reversion window")

        capital = float(self._config.secrets.initial_capital)
        max_loss = capital * float(self._config.get("intraday.max_daily_loss_pct", 1.5) or 1.5) / 100.0
        if daily_pnl <= -max_loss:
            veto_reasons.append("daily loss limit")

        max_trend = float(
            (self._config.get("intraday.stocks_in_play", {}) or {}).get(
                "max_trend_cleanliness", 0.45
            )
            or 0.45
        )
        signal_trend = IntradayRanker.trend_cleanliness(
            frame, self._config, pd.Timestamp(as_of)
        )
        if signal_trend > max_trend:
            veto_reasons.append(f"trend too high {signal_trend:.2f}")

        min_conf = float(self._config.get("intraday.agent.min_trade_confidence", 0.60) or 0.60)
        conf = float(watchlist_entry.get("confidence", 0.0)) if watchlist_entry else 0.0
        if conf < min_conf:
            veto_reasons.append("fade confidence too low")

        risk = signal.risk_per_share
        reward = abs(signal.target_price - signal.entry_price)
        notional = signal.entry_price * max(1, int(capital * 0.004 / max(risk, 0.01)))
        cost_pct = (estimated_costs / notional * 100.0) if notional > 0 else 0.0
        net_reward = reward - signal.entry_price * cost_pct / 100.0
        min_rr = float(self._config.get("intraday.agent.min_net_rr", 1.2) or 1.2)
        if risk > 0 and net_reward / risk < min_rr:
            veto_reasons.append(f"R:R after costs {net_reward / risk:.2f} < {min_rr}")

        stop_pct = risk / signal.entry_price * 100.0 if signal.entry_price > 0 else 0.0
        min_stop_pct = float(self._config.get("intraday.min_stop_pct", 0.25) or 0.25)
        if stop_pct < min_stop_pct:
            veto_reasons.append(f"stop too tight ({stop_pct:.2f}% < {min_stop_pct:.2f}%)")

        decision = "veto" if veto_reasons else "approve"
        return {
            "decision": decision,
            "symbol": signal.symbol,
            "strategy": signal.strategy,
            "side": signal.side,
            "adjusted_stop": None,
            "adjusted_target": None,
            "use_trailing_stop": decision == "approve",
            "trailing_stop_pct": float(
                self._config.get("intraday.trailing_stop_pct", 0.25) or 0.25
            ),
            "confidence": conf or (0.7 if decision == "approve" else 0.3),
            "rationale": "; ".join(veto_reasons) if veto_reasons else "Reversion gate passed",
            "generated_by_llm": False,
        }

    def _flux_gate_rules(
        self,
        signal: IntradaySignal,
        ranked: RankedStock,
        frame: pd.DataFrame,
        as_of: datetime,
        daily_pnl: float,
        watchlist_entry: Optional[Dict[str, object]],
        estimated_costs: float,
    ) -> dict:
        from src.intraday.ofi import ofi_divergence

        veto_reasons: List[str] = []
        flow_quality = str(watchlist_entry.get("flow_quality", "noise")) if watchlist_entry else "noise"
        if signal.strategy == "ofi_momentum" and flow_quality == "noise":
            veto_reasons.append("noise flow")

        if signal.strategy == "ofi_momentum" and ofi_divergence(frame):
            veto_reasons.append("OFI price divergence")

        if ranked.range_pct > float(self._config.get("intraday.flux_max_atr_pct", 1.5) or 1.5):
            veto_reasons.append("ATR too wide")

        end_parts = str(self._config.get("intraday.trade_window_end", "14:30")).split(":")
        window_end = time(int(end_parts[0]), int(end_parts[1]))
        if as_of.time() >= window_end:
            veto_reasons.append("past trade window end")

        capital = float(self._config.secrets.initial_capital)
        max_loss = capital * float(self._config.get("intraday.max_daily_loss_pct", 0.8) or 0.8) / 100.0
        if daily_pnl <= -max_loss:
            veto_reasons.append("daily loss limit")

        risk = signal.risk_per_share
        reward = abs(signal.target_price - signal.entry_price)
        agent_cfg = self._config.get("intraday.agent", {}) or {}
        if signal.strategy == "ofi_momentum":
            min_rr = float(agent_cfg.get("min_net_rr_momentum", 1.3) or 1.3)
        else:
            min_rr = float(agent_cfg.get("min_net_rr", 1.0) or 1.0)
        notional = signal.entry_price * max(1, int(capital * 0.004 / max(risk, 0.01)))
        cost_pct = (estimated_costs / notional * 100.0) if notional > 0 else 0.0
        net_reward = reward - signal.entry_price * cost_pct / 100.0
        if risk > 0 and net_reward / risk < min_rr:
            veto_reasons.append(f"R:R after costs {net_reward / risk:.2f} < {min_rr}")

        stop_pct = risk / signal.entry_price * 100.0 if signal.entry_price > 0 else 0.0
        min_stop_pct = float(self._config.get("intraday.min_stop_pct", 0.12) or 0.12)
        if stop_pct < min_stop_pct:
            veto_reasons.append("stop too tight")

        min_conf = float(agent_cfg.get("min_trade_confidence", 0.50) or 0.50)
        conf = float(watchlist_entry.get("confidence", 0.0)) if watchlist_entry else 0.0
        if conf < min_conf:
            veto_reasons.append("flow confidence too low")

        decision = "veto" if veto_reasons else "approve"
        return {
            "decision": decision,
            "symbol": signal.symbol,
            "strategy": signal.strategy,
            "side": signal.side,
            "adjusted_stop": None,
            "adjusted_target": None,
            "use_trailing_stop": decision == "approve",
            "trailing_stop_pct": float(
                self._config.get("intraday.trailing_stop_pct", 0.25) or 0.25
            ),
            "confidence": conf or (0.7 if decision == "approve" else 0.3),
            "rationale": "; ".join(veto_reasons) if veto_reasons else "Toxicity gate passed",
            "generated_by_llm": False,
        }

    def _spread_gate_rules(
        self,
        signal: IntradaySignal,
        ranked: RankedStock,
        as_of: datetime,
        daily_pnl: float,
        watchlist_entry: Optional[Dict[str, object]],
    ) -> dict:
        veto_reasons: List[str] = []
        end_parts = str(self._config.get("intraday.trade_window_end", "14:00")).split(":")
        window_end = time(int(end_parts[0]), int(end_parts[1]))
        if as_of.time() >= window_end:
            veto_reasons.append("past pair window")

        capital = float(self._config.secrets.initial_capital)
        max_loss = capital * float(self._config.get("intraday.max_daily_loss_pct", 1.5) or 1.5) / 100.0
        if daily_pnl <= -max_loss:
            veto_reasons.append("daily loss limit")

        if not watchlist_entry or not watchlist_entry.get("pair_id"):
            veto_reasons.append("missing pair context")

        conf = float(watchlist_entry.get("confidence", 0.0)) if watchlist_entry else 0.0
        decision = "veto" if veto_reasons else "approve"
        return {
            "decision": decision,
            "symbol": signal.symbol,
            "strategy": signal.strategy,
            "side": signal.side,
            "adjusted_stop": None,
            "adjusted_target": None,
            "use_trailing_stop": False,
            "trailing_stop_pct": None,
            "confidence": conf or (0.7 if decision == "approve" else 0.3),
            "rationale": "; ".join(veto_reasons) if veto_reasons else "Convergence gate passed",
            "generated_by_llm": False,
        }

    def _xstat_gate_rules(
        self,
        signal: IntradaySignal,
        ranked: RankedStock,
        as_of: datetime,
        daily_pnl: float,
        watchlist_entry: Optional[Dict[str, object]],
    ) -> dict:
        gate = self._spread_gate_rules(signal, ranked, as_of, daily_pnl, watchlist_entry)
        end_parts = str(self._config.get("intraday.trade_window_end", "14:00")).split(":")
        window_end = time(int(end_parts[0]), int(end_parts[1]))
        if as_of.time() >= window_end and gate["decision"] == "approve":
            gate["decision"] = "veto"
            gate["rationale"] = "past cross-stat window"
        return gate

    def _trade_confidence(
        self,
        watchlist_entry: Optional[Dict[str, object]],
        ranked: RankedStock,
        style: Optional[object] = None,
    ) -> float:
        """Confidence for the trade gate — watchlist entry or quant fallback."""
        if watchlist_entry is not None:
            conf = float(watchlist_entry.get("confidence", 0.0) or 0.0)
        elif not ranked.in_play:
            return 0.0
        else:
            sip = self._config.get("intraday.stocks_in_play", {}) or {}
            min_gap = float(sip.get("min_gap_pct", 0.8) or 0.8)
            conf = 0.55 + ranked.trend_cleanliness * 0.3
            if ranked.volume_expansion >= float(sip.get("min_rvol", 1.5) or 1.5):
                conf += 0.05
            if ranked.gap_pct is not None and abs(ranked.gap_pct) >= min_gap:
                conf += 0.10

        if style is None:
            style = get_style_profile(self._config, ranked.symbol)
        fund_cfg = self._config.get("intraday.fundamentals", {}) or {}
        if style is not None and fund_cfg.get("enabled"):
            boost = float(fund_cfg.get("growth_confidence_boost", 0.05) or 0.05)
            penalty = float(fund_cfg.get("value_confidence_penalty", 0.08) or 0.08)
            if style.intraday_bias == "momentum" and style.label == "growth":
                conf = min(0.95, conf + boost)
            elif style.intraday_bias == "mean_reversion" and style.label in (
                "value",
                "value_trap",
            ):
                conf = max(0.0, conf - penalty)
        return round(min(conf, 0.95), 2)

    def _slice_gate_rules(
        self,
        signal: IntradaySignal,
        as_of: datetime,
        daily_pnl: float,
    ) -> dict:
        veto_reasons: List[str] = []
        capital = float(self._config.secrets.initial_capital)
        max_loss = capital * float(self._config.get("intraday.max_daily_loss_pct", 1.5) or 1.5) / 100.0
        if daily_pnl <= -max_loss:
            veto_reasons.append("daily loss limit")
        decision = "veto" if veto_reasons else "approve"
        return {
            "decision": decision,
            "symbol": signal.symbol,
            "strategy": signal.strategy,
            "side": signal.side,
            "adjusted_stop": None,
            "adjusted_target": None,
            "use_trailing_stop": False,
            "trailing_stop_pct": None,
            "confidence": 1.0,
            "rationale": "; ".join(veto_reasons) if veto_reasons else "Predetermined slice",
            "generated_by_llm": False,
        }
