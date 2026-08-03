"""Intraday LLM agents with rules-only fallback."""

from __future__ import annotations

from datetime import datetime, time
from typing import Dict, List, Optional

import pandas as pd

from src.config import Config, get_config
from src.intraday.briefing import build_catalyst_briefing, build_trade_gate_briefing
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
        if self.enabled and self._llm.available:
            result = self._catalyst_llm(ranked, frames, as_of, daily_pnl)
            if result is not None:
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
        if self.enabled and self._llm.available:
            result = self._gate_llm(
                signal, ranked, frame, as_of, daily_pnl, watchlist_entry, estimated_costs
            )
            if result is not None:
                return result
        return self._gate_rules(
            signal, ranked, frame, as_of, daily_pnl, watchlist_entry, estimated_costs
        )

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
        if persona_id == "revert":
            return self._revert_catalyst_rules(ranked)
        if persona_id == "flux":
            return self._flux_catalyst_rules(ranked)
        if persona_id == "spread":
            return self._spread_catalyst_rules(ranked)
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
            watchlist.append(
                {
                    "symbol": item.symbol,
                    "in_play": True,
                    "catalyst_class": catalyst,
                    "direction_bias": "either",
                    "confidence": round(min(conf, 0.95), 2),
                    "rationale": f"Quant in_play: {'; '.join(item.reasons[:3])}",
                }
            )
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
        if regime != "trend":
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
        min_conf = float(self._config.get("intraday.agent.min_watch_confidence", 0.65) or 0.65)
        watchlist = []
        for item in ranked[:10]:
            conf = min(0.95, 0.5 + item.score / 100.0)
            watchlist.append(
                {
                    "symbol": item.symbol,
                    "flow_quality": "informed",
                    "in_play": True,
                    "direction_bias": "either",
                    "ofi_z": 0.0,
                    "confidence": round(conf, 2),
                    "rationale": "Rules-only flow placeholder (needs 1-min OFI frame).",
                }
            )
            if len(watchlist) >= 3:
                break
        quality = "fair" if watchlist else "poor"
        return {
            "flow_summary": "Rules-only flow scan.",
            "session_view": "Rules-only flow scan.",
            "trade_day_quality": quality,
            "watchlist": [w for w in watchlist if w["confidence"] >= min_conf],
            "generated_by_llm": False,
        }

    def _spread_catalyst_rules(self, ranked: List[RankedStock]) -> dict:
        pairs = self._config.get("intraday.pairs") or []
        opportunities = []
        watchlist = []
        for pair in pairs:
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                continue
            leg_a, leg_b = str(pair[0]).upper(), str(pair[1]).upper()
            opportunities.append(
                {
                    "leg_a": leg_a,
                    "leg_b": leg_b,
                    "spread_z": 2.0,
                    "divergence_type": "technical",
                    "cointegration_ok": True,
                    "direction": "short_spread",
                    "confidence": 0.6,
                    "rationale": "Rules-only spread placeholder.",
                }
            )
            for leg in (leg_a, leg_b):
                watchlist.append(
                    {
                        "symbol": leg,
                        "in_play": True,
                        "direction_bias": "either",
                        "confidence": 0.6,
                        "pair_id": f"{leg_a}:{leg_b}",
                        "rationale": "Rules-only spread placeholder.",
                    }
                )
            if len(opportunities) >= 2:
                break
        return {
            "session_view": "Rules-only spread scan.",
            "trade_day_quality": "fair" if opportunities else "poor",
            "pair_opportunities": opportunities,
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
        veto_reasons: List[str] = []
        if signal.strategy == "scalping" or self._config.get("intraday.agent.veto_scalping", True):
            if signal.strategy == "scalping":
                veto_reasons.append("scalping disabled")

        end_parts = str(self._config.get("intraday.trade_window_end", "14:30")).split(":")
        window_end = time(int(end_parts[0]), int(end_parts[1]))
        if as_of.time() >= window_end:
            veto_reasons.append("past trade window end")

        capital = float(self._config.secrets.initial_capital)
        max_loss = capital * float(self._config.get("intraday.max_daily_loss_pct", 1.5) or 1.5) / 100.0
        if daily_pnl <= -max_loss:
            veto_reasons.append("daily loss limit")

        min_conf = float(self._config.get("intraday.agent.min_trade_confidence", 0.55) or 0.55)
        conf = float(watchlist_entry.get("confidence", 0.0)) if watchlist_entry else 0.0
        if ranked.in_play and conf < min_conf:
            veto_reasons.append("catalyst confidence too low")

        risk = signal.risk_per_share
        reward = abs(signal.target_price - signal.entry_price)
        notional = signal.entry_price * max(1, int(capital * 0.004 / max(risk, 0.01)))
        cost_pct = (estimated_costs / notional * 100.0) if notional > 0 else 0.0
        net_reward = reward - signal.entry_price * cost_pct / 100.0
        min_rr = 1.2 if signal.strategy == "opening_range_breakout" else 1.5
        if risk > 0 and net_reward / risk < min_rr:
            veto_reasons.append(f"R:R after costs {net_reward / risk:.2f} < {min_rr}")

        stop_pct = risk / signal.entry_price * 100.0 if signal.entry_price > 0 else 0.0
        if stop_pct < 0.15:
            veto_reasons.append("stop too tight")

        if not ranked.in_play:
            veto_reasons.append("not in_play")

        sip = self._config.get("intraday.stocks_in_play", {}) or {}
        min_signal_trend = float(
            sip.get(
                "min_trend_at_signal",
                sip.get("min_trend_cleanliness", 0.5),
            )
            or 0.5
        )
        signal_trend = IntradayRanker.trend_cleanliness(
            frame, self._config, pd.Timestamp(as_of)
        )
        if signal_trend < min_signal_trend:
            veto_reasons.append(
                f"trend at signal {signal_trend:.2f} < {min_signal_trend:.2f}"
            )

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
        if regime == "trend":
            veto_reasons.append("trend regime")

        if as_of.time() >= time(14, 0):
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
        if risk > 0 and net_reward / risk < 1.0:
            veto_reasons.append(f"R:R after costs {net_reward / risk:.2f} < 1.0")

        stop_pct = risk / signal.entry_price * 100.0 if signal.entry_price > 0 else 0.0
        if stop_pct < 0.12:
            veto_reasons.append("stop too tight")

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

        if ranked.range_pct > 1.2:
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
        min_rr = 1.0 if signal.strategy == "ofi_momentum" else 0.8
        notional = signal.entry_price * max(1, int(capital * 0.004 / max(risk, 0.01)))
        cost_pct = (estimated_costs / notional * 100.0) if notional > 0 else 0.0
        net_reward = reward - signal.entry_price * cost_pct / 100.0
        if risk > 0 and net_reward / risk < min_rr:
            veto_reasons.append(f"R:R after costs {net_reward / risk:.2f} < {min_rr}")

        stop_pct = risk / signal.entry_price * 100.0 if signal.entry_price > 0 else 0.0
        if stop_pct < 0.10:
            veto_reasons.append("stop too tight")

        conf = float(watchlist_entry.get("confidence", 0.0)) if watchlist_entry else 0.0
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
        if as_of.time() >= time(14, 0):
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
