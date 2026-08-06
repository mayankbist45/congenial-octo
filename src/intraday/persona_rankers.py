"""Persona-specific intraday rankers."""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

import pandas as pd

from src.config import Config
from src.intraday.models import RankedStock
from src.intraday.cross_stat_math import configured_cross_baskets, residual_z_at
from src.intraday.ofi import ofi_divergence, ofi_z_proxy
from src.intraday.ranking import IntradayRanker
from src.intraday.spread_math import configured_pairs, spread_z_at


class RevertRanker(IntradayRanker):
    """Fade candidates: far from VWAP, low trend, small gaps."""

    def _score(
        self,
        symbol: str,
        frame: pd.DataFrame,
        previous_day: Optional[Dict[str, float]],
    ) -> Optional[RankedStock]:
        if frame.empty or len(frame) < 6:
            return None

        min_volume = float(self._config.get("intraday.min_avg_volume", 100000) or 100000)
        min_turnover = float(
            self._config.get("intraday.min_avg_turnover_inr", 1e8) or 1e8
        )
        min_range = float(self._config.get("intraday.min_atr_pct", 0.6) or 0.6)
        sip = self._config.get("intraday.stocks_in_play", {}) or {}
        max_gap = float(sip.get("max_gap_pct", 0.8) or 0.8)
        max_trend = float(sip.get("max_trend_cleanliness", 0.45) or 0.45)

        close = float(frame["close"].iloc[-1])
        first = float(frame["open"].iloc[0])
        turnover = float((frame["close"] * frame["volume"]).sum())
        avg_volume = float(frame["volume"].mean())
        recent_volume = float(frame["volume"].tail(3).mean())
        volume_expansion = recent_volume / avg_volume if avg_volume > 0 else 0.0
        range_pct = (float(frame["high"].max()) / float(frame["low"].min()) - 1.0) * 100.0
        gap_pct = None
        if previous_day and previous_day.get("close"):
            gap_pct = (first / float(previous_day["close"]) - 1.0) * 100.0
        vwap = self._vwap(frame)
        vwap_distance_pct = (close / vwap - 1.0) * 100.0 if vwap else None
        prior_distance = self._prior_level_distance(close, previous_day)
        trend_cleanliness = self.trend_cleanliness(frame, self._config)

        if avg_volume < min_volume or turnover < min_turnover or range_pct < min_range:
            return None
        if gap_pct is not None and abs(gap_pct) > max_gap:
            return None
        if trend_cleanliness > max_trend:
            return None

        vwap_dist = abs(vwap_distance_pct or 0.0)
        vwap_score = min(vwap_dist, 3.0) * 25.0
        liquidity = min(turnover / min_turnover, 4.0) * 20.0
        range_score = min(range_pct / max(min_range, 0.01), 3.0) * 15.0
        anti_trend = (1.0 - trend_cleanliness) * 25.0
        low_gap_bonus = 10.0 if abs(gap_pct or 0.0) < 0.5 else 0.0
        calan_penalty = 0.0
        if gap_pct is not None and abs(gap_pct) >= 0.8 and volume_expansion >= 1.5:
            calan_penalty = 20.0
        score = vwap_score + liquidity + range_score + anti_trend + low_gap_bonus - calan_penalty

        return RankedStock(
            symbol=symbol,
            score=round(score, 3),
            turnover=turnover,
            volume_expansion=volume_expansion,
            range_pct=range_pct,
            gap_pct=gap_pct,
            vwap_distance_pct=vwap_distance_pct,
            prior_level_distance_pct=prior_distance,
            trend_cleanliness=trend_cleanliness,
            in_play=False,
            reasons=[
                f"vwap_dist {vwap_dist:.2f}%",
                f"trend {trend_cleanliness:.2f}",
                "fade_candidate",
            ],
        )


class FluxRanker(IntradayRanker):
    """Top liquid names ranked by |OFI z| × turnover."""

    def _score(
        self,
        symbol: str,
        frame: pd.DataFrame,
        previous_day: Optional[Dict[str, float]],
    ) -> Optional[RankedStock]:
        item = super()._score(symbol, frame, previous_day)
        if item is None:
            return None

        lookback = int(self._config.get("intraday.ofi_lookback_bars", 5) or 5)
        z_lookback = int(self._config.get("intraday.ofi_z_lookback", 60) or 60)
        ofi_z = ofi_z_proxy(frame, lookback=lookback, z_lookback=z_lookback)
        divergence = ofi_divergence(frame)

        ofi_score = min(abs(ofi_z), 4.0) * 30.0
        turnover_score = min(item.turnover / 1e8, 5.0) * 25.0
        tight_spread = max(0.0, 2.0 - item.range_pct) * 15.0
        alignment = -15.0 if divergence else 10.0
        score = ofi_score + turnover_score + tight_spread + alignment

        z_entry = float(self._config.get("intraday.ofi_z_entry", 1.5) or 1.5)
        in_play = abs(ofi_z) >= z_entry * 0.85 and not divergence

        return RankedStock(
            symbol=item.symbol,
            score=round(score, 3),
            turnover=item.turnover,
            volume_expansion=item.volume_expansion,
            range_pct=item.range_pct,
            gap_pct=item.gap_pct,
            vwap_distance_pct=item.vwap_distance_pct,
            prior_level_distance_pct=item.prior_level_distance_pct,
            trend_cleanliness=item.trend_cleanliness,
            in_play=in_play,
            ofi_z=round(ofi_z, 3),
            reasons=[
                f"ofi_z {ofi_z:+.2f}",
                f"divergence={divergence}",
                f"turnover Rs {item.turnover:,.0f}",
            ],
        )


class SpreadRanker(IntradayRanker):
    """Rank pair legs by spread z-score opportunity."""

    def rank(
        self,
        frames: Dict[str, pd.DataFrame],
        previous_days: Optional[Dict[str, Dict[str, float]]] = None,
    ) -> List[RankedStock]:
        previous_days = previous_days or {}
        ranked: List[RankedStock] = []
        seen: set[str] = set()
        for leg_a, leg_b in configured_pairs(self._config):
            fa = frames.get(leg_a)
            fb = frames.get(leg_b)
            if fa is None or fb is None or fa.empty or fb.empty:
                continue
            min_bars = int(self._config.get("intraday.pair_min_bars", 20) or 20)
            z = spread_z_at(fa, fb, min_bars=min_bars)
            if z is None:
                continue
            turnover_a = float((fa["close"] * fa["volume"]).sum())
            turnover_b = float((fb["close"] * fb["volume"]).sum())
            liquidity = min(min(turnover_a, turnover_b) / 1e8, 4.0) * 25.0
            spread_score = min(abs(z), 4.0) * 35.0
            hist = spread_z_at(fa.iloc[:-3], fb.iloc[:-3]) if len(fa) > 33 else z
            stable = 15.0 if hist is not None and abs(z - hist) <= 0.3 else -10.0
            score = spread_score + liquidity + stable
            pair_id = f"{leg_a}:{leg_b}"
            for symbol, frame, turnover in (
                (leg_a, fa, turnover_a),
                (leg_b, fb, turnover_b),
            ):
                if symbol in seen:
                    continue
                seen.add(symbol)
                trend = self.trend_cleanliness(frame, self._config)
                ranked.append(
                    RankedStock(
                        symbol=symbol,
                        score=round(score, 3),
                        turnover=turnover,
                        volume_expansion=1.0,
                        range_pct=(float(frame["high"].max()) / float(frame["low"].min()) - 1.0)
                        * 100.0,
                        gap_pct=None,
                        vwap_distance_pct=None,
                        prior_level_distance_pct=None,
                        trend_cleanliness=trend,
                        in_play=abs(z) >= float(
                            self._config.get("intraday.pair_entry_z", 2.0) or 2.0
                        ),
                        spread_z=round(z, 3),
                        pair_id=pair_id,
                        reasons=[f"pair {pair_id}", f"spread_z {z:+.2f}"],
                    )
                )
        ranked.sort(key=lambda r: r.score, reverse=True)
        return ranked


class XStatRanker(IntradayRanker):
    """Rank cross baskets by beta-hedged residual z-score."""

    def rank(
        self,
        frames: Dict[str, pd.DataFrame],
        previous_days: Optional[Dict[str, Dict[str, float]]] = None,
    ) -> List[RankedStock]:
        previous_days = previous_days or {}
        ranked: List[RankedStock] = []
        seen: set[str] = set()
        min_bars = int(self._config.get("intraday.cross_min_bars", 25) or 25)
        beta_lookback = int(self._config.get("intraday.cross_beta_lookback", 60) or 60)
        entry_z = float(self._config.get("intraday.cross_entry_z", 2.0) or 2.0)

        for basket in configured_cross_baskets(self._config):
            anchor = basket.anchor
            leg = basket.leg
            fa = frames.get(anchor)
            fb = frames.get(leg)
            if fa is None or fb is None or fa.empty or fb.empty:
                continue
            z = residual_z_at(
                fa,
                fb,
                beta=basket.beta,
                beta_lookback=beta_lookback,
                min_bars=min_bars,
            )
            if z is None:
                continue
            turnover_a = float((fa["close"] * fa["volume"]).sum())
            turnover_b = float((fb["close"] * fb["volume"]).sum())
            liquidity = min(min(turnover_a, turnover_b) / 1e8, 4.0) * 25.0
            spread_score = min(abs(z), 4.0) * 35.0
            score = spread_score + liquidity
            pair_id = basket.basket_id
            for symbol, frame, turnover in (
                (anchor, fa, turnover_a),
                (leg, fb, turnover_b),
            ):
                if symbol in seen:
                    continue
                seen.add(symbol)
                trend = self.trend_cleanliness(frame, self._config)
                ranked.append(
                    RankedStock(
                        symbol=symbol,
                        score=round(score, 3),
                        turnover=turnover,
                        volume_expansion=1.0,
                        range_pct=(float(frame["high"].max()) / float(frame["low"].min()) - 1.0)
                        * 100.0,
                        gap_pct=None,
                        vwap_distance_pct=None,
                        prior_level_distance_pct=None,
                        trend_cleanliness=trend,
                        in_play=abs(z) >= entry_z,
                        spread_z=round(z, 3),
                        pair_id=pair_id,
                        reasons=[f"basket {pair_id}", f"residual_z {z:+.2f}"],
                    )
                )
        ranked.sort(key=lambda r: r.score, reverse=True)
        return ranked


class SliceRanker(IntradayRanker):
    """Rank symbols with active parent orders."""

    def rank(
        self,
        frames: Dict[str, pd.DataFrame],
        previous_days: Optional[Dict[str, Dict[str, float]]] = None,
    ) -> List[RankedStock]:
        orders = self._config.get("intraday.parent_orders") or []
        ranked: List[RankedStock] = []
        for order in orders:
            if not isinstance(order, dict):
                continue
            symbol = str(order.get("symbol", "")).upper()
            frame = frames.get(symbol)
            if not frame or frame.empty:
                continue
            turnover = float((frame["close"] * frame["volume"]).sum())
            ranked.append(
                RankedStock(
                    symbol=symbol,
                    score=100.0,
                    turnover=turnover,
                    volume_expansion=1.0,
                    range_pct=0.0,
                    gap_pct=None,
                    vwap_distance_pct=None,
                    prior_level_distance_pct=None,
                    trend_cleanliness=0.0,
                    in_play=True,
                    reasons=[f"parent_order {order.get('side', 'BUY')}"],
                )
            )
        return ranked


def load_ranker(config: Config) -> IntradayRanker:
    persona = config.persona_id
    if persona == "revert":
        return RevertRanker(config)
    if persona == "flux":
        return FluxRanker(config)
    if persona == "spread":
        return SpreadRanker(config)
    if persona == "xstat":
        return XStatRanker(config)
    if persona == "slice":
        return SliceRanker(config)
    return IntradayRanker(config)


def filter_ranked_for_persona(config: Config, ranked: List[RankedStock]) -> List[RankedStock]:
    """Pre-catalyst universe filter; calan personas keep in_play names only."""
    from src.intraday.mayank import is_mayank_persona
    from src.market.registry import persona_strategy_type

    if is_mayank_persona(config):
        return ranked
    if persona_strategy_type(config.persona_id) == "calan":
        return [r for r in ranked if r.in_play]
    return ranked
