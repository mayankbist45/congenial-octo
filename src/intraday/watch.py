"""Live market-hours intraday loop (paper sim or broker-routed orders)."""

from __future__ import annotations

import time as time_module
from dataclasses import asdict
from datetime import date, datetime, time, timedelta
from typing import Callable, Dict, Iterable, List, Optional, Set

import pandas as pd

from src.config import Config
from src.intraday.agent import IntradayAgent
from src.intraday.broker_execution import IntradayBrokerExecutor, intraday_uses_broker
from src.intraday.data import IntradayDataService
from src.intraday.engine_helpers import collect_signals
from src.intraday.mayank import (
    build_style_context,
    enrich_candidates_with_style,
    is_mayank_persona,
)
from src.intraday.models import IntradaySignal, IntradayTrade, PaperPosition, PaperState, RankedStock
from src.intraday.pair_exits import is_pair_strategy, z_at_bar, pair_exit_reason, pair_z_config_keys
from src.intraday.persona_rankers import filter_ranked_for_persona, load_ranker
from src.intraday.reporting import save_summary, summarize_state
from src.intraday.state import PaperStateStore
from src.intraday.strategies import load_strategies
from src.logging_setup import get_logger
from src.market.session import market_timezone, session_open_close
from src.operations import log_structured_event

logger = get_logger(__name__)


def parse_hhmm(value: str) -> time:
    hour, minute = str(value).split(":", 1)
    return time(int(hour), int(minute))


def _ts_for_index(when: datetime, index: pd.DatetimeIndex, config: Config) -> pd.Timestamp:
    """Align an aware ``now`` with exchange-local naive candle indices."""
    ts = pd.Timestamp(when)
    if not isinstance(index, pd.DatetimeIndex) or index.tz is not None:
        return ts
    if ts.tz is not None:
        return ts.tz_convert(market_timezone(config)).tz_localize(None)
    return ts


def _naive_exchange_dt(when: datetime, tz) -> datetime:
    """Normalize to exchange-local naive time for safe datetime comparisons."""
    if when.tzinfo is not None:
        return when.astimezone(tz).replace(tzinfo=None)
    return when


class IntradayWatchEngine:
    def __init__(
        self,
        config: Config,
        data_service: IntradayDataService,
        symbols: Iterable[str],
        state_store: Optional[PaperStateStore] = None,
        agent: Optional[IntradayAgent] = None,
        now: Optional[Callable[[], datetime]] = None,
        sleep: Optional[Callable[[float], None]] = None,
    ):
        self._config = config
        self._data = data_service
        self._symbols = list(symbols)
        self._state_store = state_store or PaperStateStore(config)
        self._agent = agent or IntradayAgent(config)
        self._tz = market_timezone(config)
        if now is None:
            self._now = lambda: datetime.now(self._tz)
        else:
            self._now = now
        self._sleep = sleep or time_module.sleep
        self._ranker = load_ranker(config)
        self._strategies = load_strategies(config)
        self._catalyst_scanned = False
        self._broker_exec: Optional[IntradayBrokerExecutor] = None
        self._last_heartbeat_at: Optional[datetime] = None
        self._heartbeat_interval_seconds = max(
            int(self._config.get("observability.heartbeat_interval_seconds", 300) or 300),
            30,
        )

    def run_until_close(self) -> PaperState:
        force_exit = self._force_exit_time()
        self._emit_heartbeat("starting", force=True)
        while True:
            now = self._exchange_now(self._now())
            state = self.run_once(now)
            self._emit_heartbeat("running", now=now, state=state)
            if now.time() >= force_exit:
                self._emit_heartbeat("stopping", now=now, state=state, force=True)
                state.active = False
                self._save(state)
                self._emit_heartbeat("stopped", now=now, state=state, force=True)
                return state
            self._sleep(self._poll_seconds(state))

    def run_once(self, now: Optional[datetime] = None) -> PaperState:
        now = self._exchange_now(now or self._now())
        state = self._state_store.load(now.date())
        state.active = self._is_market_watch_time(now)
        state.last_scan_at = now.isoformat(timespec="seconds")

        symbols = self._symbols_for_poll(state, now)
        frames = self._load_frames(now, symbols)
        interval = str(self._config.get("intraday.default_interval", "FIVE_MINUTE")).upper()
        previous_days = self._data.previous_day_stats_batch(
            list(frames.keys()), now.date(), interval=interval
        )
        self._close_positions(state, frames, now)
        ranked = self._ranker.rank(frames, previous_days)
        limit = int(self._config.get("intraday.ranked_universe_size", 8) or 8)
        ranked = ranked[:limit]
        state.top_candidates = [asdict(item) for item in ranked]

        if is_mayank_persona(self._config):
            profiles, params = build_style_context(self._config, ranked)
            state.style_profiles = profiles
            state.style_params = params
            enrich_candidates_with_style(state.top_candidates, profiles)

        if now.time() >= self._catalyst_scan_time() and (
            not self._catalyst_scanned or (not state.watchlist and any(r.in_play for r in ranked))
        ):
            pool = filter_ranked_for_persona(self._config, ranked)
            if not pool:
                pool = ranked
            catalyst = self._agent.catalyst_scan(pool or ranked, frames, now, state.daily_pnl)
            watchlist = list(catalyst.get("watchlist") or [])
            if watchlist:
                state.watchlist = watchlist
            elif not self._catalyst_scanned and not state.watchlist:
                state.watchlist = watchlist
            self._catalyst_scanned = True

        if self._can_enter_new_trades(state, now):
            watch_symbols = self._watchlist_symbols(state)
            if self._uses_watchlist_gate():
                if watch_symbols:
                    ranked = [r for r in ranked if r.symbol in watch_symbols]
                    self._open_new_positions(state, frames, ranked, now, previous_days)
            else:
                self._open_new_positions(state, frames, ranked, now, previous_days)

        self._apply_daily_lockout(state)
        self._save(state)
        return state

    def _symbols_for_poll(self, state: PaperState, now: datetime) -> List[str]:
        must = [p.symbol for p in state.open_positions]
        must.extend(
            str(w["symbol"]).upper()
            for w in state.watchlist
            if w.get("in_play") and w.get("symbol")
        )
        must = list(dict.fromkeys(must))

        if not self._symbols:
            return must

        # Active phase: after catalyst, poll watchlist + positions only.
        if self._catalyst_scanned and must:
            active_cap = int(
                self._config.get("intraday.watch_active_scan_size", 6) or 6
            )
            return must[:active_cap]

        # Empty watchlist after catalyst: limited discovery retry, not full universe.
        if self._catalyst_scanned and not state.watchlist:
            scan_size = int(
                self._config.get("intraday.watch_discovery_scan_size", 8) or 8
            )
        else:
            scan_size = int(
                self._config.get(
                    "intraday.watch_discovery_scan_size",
                    self._config.get("intraday.watch_scan_size", 8),
                )
                or 8
            )

        poll = max(int(self._config.get("intraday.watch_poll_seconds", 300) or 300), 1)
        offset = int(now.timestamp() // poll) % len(self._symbols)
        rotated = self._symbols[offset:] + self._symbols[:offset]
        selected: List[str] = []
        must_set = set(must)
        for symbol in rotated:
            if symbol in must_set:
                continue
            selected.append(symbol)
            if len(selected) >= scan_size:
                break
        return list(dict.fromkeys(must + selected))

    def _load_frames(self, now: datetime, symbols: Iterable[str]) -> Dict[str, pd.DataFrame]:
        interval = str(self._config.get("intraday.default_interval", "FIVE_MINUTE")).upper()
        frames: Dict[str, pd.DataFrame] = {}
        for symbol in symbols:
            try:
                frame = self._data.candles(
                    symbol, now.date(), interval=interval, use_cache=True, refresh=True
                )
            except Exception:
                continue
            if not frame.empty:
                ts = _ts_for_index(now, frame.index, self._config)
                frames[symbol] = frame.loc[frame.index <= ts]
        return frames

    def _open_new_positions(
        self,
        state: PaperState,
        frames: Dict[str, pd.DataFrame],
        ranked: List[RankedStock],
        now: datetime,
        previous_days: Optional[Dict[str, Dict[str, float]]] = None,
    ) -> None:
        signals = self._rank_signals(frames, ranked, state, previous_days)
        max_trades = int(self._config.get("intraday.max_trades_per_day", 3) or 3)
        max_open = int(self._config.get("intraday.max_open_positions", 2) or 2)
        watch_map = {
            str(w["symbol"]).upper(): w for w in state.watchlist if w.get("in_play")
        }
        ranked_map = {r.symbol: r for r in ranked}

        for _, signal in signals:
            if self._already_skipped(state, signal):
                continue
            if self._uses_watchlist_gate() and signal.symbol not in watch_map:
                self._skip(state, signal, "symbol not on watchlist")
                continue
            if self._is_stale_signal(signal, now, state):
                self._skip(state, signal, "stale signal (missed entry window)")
                continue
            if state.trade_count >= max_trades:
                self._skip(state, signal, "max trades reached")
                continue
            needed = 2 if signal.is_pair else 1
            if len(state.open_positions) + needed > max_open:
                self._skip(state, signal, "max open positions reached")
                continue
            symbols = [signal.symbol]
            if signal.pair_leg:
                symbols.append(signal.pair_leg)
            if any(p.symbol in symbols for p in state.open_positions):
                self._skip(state, signal, "symbol already has an open paper position")
                continue
            if any(t.symbol in symbols for t in state.completed_trades):
                self._skip(state, signal, "symbol already traded today")
                continue

            item = ranked_map.get(signal.symbol)
            frame = frames.get(signal.symbol)
            if item is None or frame is None:
                self._skip(state, signal, "missing rank or candles")
                continue

            est_costs = self._estimate_costs(signal, state)
            gate = self._agent.gate_trade(
                signal,
                item,
                frame,
                now,
                state.daily_pnl,
                watch_map.get(signal.symbol),
                est_costs,
            )
            if gate.get("decision") != "approve":
                self._skip(state, signal, gate.get("rationale", "agent veto"))
                continue
            if gate.get("adjusted_stop"):
                signal.stop_price = float(gate["adjusted_stop"])
            if gate.get("adjusted_target"):
                signal.target_price = float(gate["adjusted_target"])
            if gate.get("use_trailing_stop") is not None:
                signal.use_trailing_stop = bool(gate["use_trailing_stop"])

            position = self._paper_fill(signal, state, gate)
            if position is None:
                self._skip(state, signal, "position size rounded to zero")
                continue
            broker_exec = self._broker_executor()
            if broker_exec is not None:
                ok, err = broker_exec.submit_entry(position)
                if not ok:
                    self._skip(state, signal, err or "broker entry rejected")
                    continue
            state.open_positions.append(position)
            if signal.is_pair and signal.pair_leg:
                pair_position = self._paper_fill_pair_leg(signal, state, gate)
                if pair_position is None:
                    if broker_exec is not None:
                        broker_exec.flatten(position, position.entry_price)
                    state.open_positions.pop()
                    self._skip(state, signal, "pair leg size rounded to zero")
                    continue
                if broker_exec is not None:
                    ok, err = broker_exec.submit_entry(pair_position)
                    if not ok:
                        broker_exec.flatten(position, position.entry_price)
                        state.open_positions.pop()
                        self._skip(state, signal, err or "broker pair entry rejected")
                        continue
                state.open_positions.append(pair_position)

    def _rank_signals(
        self,
        frames: Dict[str, pd.DataFrame],
        ranked: List[RankedStock],
        state: PaperState,
        previous_days: Optional[Dict[str, Dict[str, float]]] = None,
    ) -> List[tuple[float, IntradaySignal]]:
        if self._uses_watchlist_gate():
            watch_symbols = self._watchlist_symbols(state)
            if watch_symbols:
                ranked = [r for r in ranked if r.symbol in watch_symbols]
        collected = collect_signals(self._config, frames, ranked, previous_days)
        return [(score, signal) for score, signal, _ in collected]

    def _paper_fill(
        self, signal: IntradaySignal, state: PaperState, gate: dict
    ) -> Optional[PaperPosition]:
        equity = self._equity(state)
        risk_amount = equity * float(
            self._config.get("intraday.risk_per_trade_pct", 0.4) or 0.4
        ) / 100.0
        max_value = equity * float(
            self._config.get("intraday.max_position_value_pct", 20.0) or 20.0
        ) / 100.0
        slippage = float(self._config.get("intraday.slippage_bps_per_side", 2.0) or 2.0) / 10000.0
        entry_price = signal.entry_price * (
            1.0 + slippage if signal.side == "BUY" else 1.0 - slippage
        )
        risk_per_share = abs(entry_price - signal.stop_price)
        if entry_price <= 0 or risk_per_share <= 0:
            return None
        quantity = int(min(risk_amount / risk_per_share, max_value / entry_price))
        if quantity < 1:
            return None
        trailing = (
            float(gate["trailing_stop_pct"])
            if gate.get("use_trailing_stop") and gate.get("trailing_stop_pct")
            else float(self._config.get("intraday.trailing_stop_pct", 0.25) or 0.25)
        )
        use_trailing = bool(gate.get("use_trailing_stop", signal.use_trailing_stop))
        return PaperPosition(
            symbol=signal.symbol,
            strategy=signal.strategy,
            side=signal.side,
            quantity=quantity,
            entry_time=signal.entry_time,
            entry_price=round(entry_price, 2),
            stop_price=signal.stop_price,
            target_price=signal.target_price,
            max_exit_time=signal.max_exit_time,
            reason=signal.reason,
            trailing_stop_pct=trailing if use_trailing else None,
            peak_price=round(entry_price, 2),
            pair_id=signal.pair_id,
            pair_partner=signal.pair_leg,
            entry_z=signal.entry_z,
            pair_beta=signal.pair_beta,
            use_trailing_stop=use_trailing,
        )

    def _paper_fill_pair_leg(
        self, signal: IntradaySignal, state: PaperState, gate: dict
    ) -> Optional[PaperPosition]:
        if not signal.pair_leg or not signal.pair_side:
            return None
        equity = self._equity(state)
        risk_amount = equity * float(
            self._config.get("intraday.risk_per_trade_pct", 0.4) or 0.4
        ) / 100.0
        max_value = equity * float(
            self._config.get("intraday.max_position_value_pct", 20.0) or 20.0
        ) / 100.0
        slippage = float(self._config.get("intraday.slippage_bps_per_side", 2.0) or 2.0) / 10000.0
        entry_price = float(signal.pair_entry_price or signal.entry_price)
        entry_price = entry_price * (
            1.0 + slippage if signal.pair_side == "BUY" else 1.0 - slippage
        )
        stop = float(signal.pair_stop_price or entry_price * 0.995)
        target = float(signal.pair_target_price or entry_price * 1.005)
        risk_per_share = abs(entry_price - stop)
        if entry_price <= 0 or risk_per_share <= 0:
            return None
        quantity = int(min(risk_amount / risk_per_share, max_value / entry_price))
        if quantity < 1:
            return None
        trailing = float(
            self._config.get("intraday.trailing_stop_pct", 0.25) or 0.25
        )
        return PaperPosition(
            symbol=signal.pair_leg,
            strategy=signal.strategy,
            side=signal.pair_side,
            quantity=quantity,
            entry_time=signal.entry_time,
            entry_price=round(entry_price, 2),
            stop_price=stop,
            target_price=target,
            max_exit_time=signal.max_exit_time,
            reason=f"{signal.reason} (pair leg)",
            trailing_stop_pct=None,
            peak_price=round(entry_price, 2),
            pair_id=signal.pair_id,
            pair_partner=signal.symbol,
            entry_z=signal.entry_z,
            pair_beta=signal.pair_beta,
            use_trailing_stop=False,
        )

    def _close_positions(
        self, state: PaperState, frames: Dict[str, pd.DataFrame], now: datetime
    ) -> None:
        still_open: List[PaperPosition] = []
        handled_pairs: set[str] = set()
        for position in state.open_positions:
            if position.pair_id and position.pair_id in handled_pairs:
                continue
            if position.pair_id and is_pair_strategy(position.strategy):
                partner = next(
                    (
                        p
                        for p in state.open_positions
                        if p.pair_id == position.pair_id and p.symbol != position.symbol
                    ),
                    None,
                )
                if partner is None:
                    still_open.append(position)
                    continue
                handled_pairs.add(position.pair_id)
                exit_payload = self._exit_for_pair(position, partner, frames, now)
                if exit_payload is None:
                    still_open.extend([position, partner])
                    continue
                for pos in (position, partner):
                    frame = frames.get(pos.symbol)
                    price = float(exit_payload["exit_prices"].get(pos.symbol, pos.entry_price))
                    trade = self._trade_from_position(
                        pos,
                        exit_time=exit_payload["exit_time"],
                        exit_price=price,
                        exit_reason=str(exit_payload["exit_reason"]),
                    )
                    trade.pair_id = position.pair_id
                    state.completed_trades.append(trade)
                    state.daily_pnl = round(state.daily_pnl + trade.net_pnl, 2)
                continue

            frame = frames.get(position.symbol)
            exit_payload = self._exit_for_position(position, frame, now)
            if exit_payload is None:
                still_open.append(position)
                continue
            broker_exec = self._broker_executor()
            if broker_exec is not None:
                ok, err = broker_exec.submit_exit(
                    position, float(exit_payload["exit_price"])
                )
                if not ok:
                    logger.warning(
                        "Broker exit pending for %s: %s", position.symbol, err
                    )
                    still_open.append(position)
                    continue
            trade = self._trade_from_position(position, **exit_payload)
            state.completed_trades.append(trade)
            state.daily_pnl = round(state.daily_pnl + trade.net_pnl, 2)
        state.open_positions = still_open

    def _exit_for_pair(
        self,
        leg_a: PaperPosition,
        leg_b: PaperPosition,
        frames: Dict[str, pd.DataFrame],
        now: datetime,
    ) -> Optional[Dict[str, object]]:
        frame_a = frames.get(leg_a.symbol)
        frame_b = frames.get(leg_b.symbol)
        if frame_a is None or frame_b is None:
            return None
        force_exit = now.time() >= parse_hhmm(
            str(self._config.get("intraday.force_exit_time", "15:15"))
        )
        exit_z, stop_z, _ = pair_z_config_keys(self._config)
        entry_ts = max(leg_a.entry_time, leg_b.entry_time)
        common = frame_a.index.intersection(frame_b.index)
        common = common[common > pd.Timestamp(entry_ts)]
        now_ts = _ts_for_index(now, frame_a.index, self._config)
        common = common[common <= now_ts]
        if common.empty:
            return None

        beta = float(leg_a.pair_beta or leg_b.pair_beta or 1.0)
        for ts in common.sort_values():
            z = z_at_bar(
                self._config,
                leg_a.strategy,
                frame_a,
                frame_b,
                ts,
                beta=beta,
            )
            if z is None:
                continue
            reason = pair_exit_reason(z, exit_z, stop_z)
            if reason:
                return {
                    "exit_time": ts.to_pydatetime(),
                    "exit_reason": reason,
                    "exit_prices": {
                        leg_a.symbol: float(frame_a.loc[ts, "close"]),
                        leg_b.symbol: float(frame_b.loc[ts, "close"]),
                    },
                }

        now_cmp = _naive_exchange_dt(now, self._tz)
        if force_exit or (
            leg_a.max_exit_time
            and now_cmp >= _naive_exchange_dt(leg_a.max_exit_time, self._tz)
        ):
            last_ts = common[-1]
            return {
                "exit_time": last_ts.to_pydatetime(),
                "exit_reason": "force_squareoff" if force_exit else "time_exit",
                "exit_prices": {
                    leg_a.symbol: float(frame_a.loc[last_ts, "close"]),
                    leg_b.symbol: float(frame_b.loc[last_ts, "close"]),
                },
            }
        return None

    def _exit_for_position(
        self,
        position: PaperPosition,
        frame: Optional[pd.DataFrame],
        now: datetime,
    ) -> Optional[Dict[str, object]]:
        force_exit = now.time() >= parse_hhmm(
            str(self._config.get("intraday.force_exit_time", "15:15"))
        )
        now_cmp = _naive_exchange_dt(now, self._tz)
        max_hold_exit = position.max_exit_time is not None and now_cmp >= _naive_exchange_dt(
            position.max_exit_time, self._tz
        )
        if frame is None or frame.empty:
            if force_exit or max_hold_exit:
                return {
                    "exit_time": now,
                    "exit_price": position.entry_price,
                    "exit_reason": "paper_time_exit_no_candle",
                }
            return None
        entry_ts = _ts_for_index(position.entry_time, frame.index, self._config)
        future = frame.loc[frame.index >= entry_ts]
        if future.empty:
            return None
        if not force_exit:
            now_ts = _ts_for_index(now, frame.index, self._config)
            future = future.loc[future.index <= now_ts]
        if future.empty:
            return None

        trailing_pct = (position.trailing_stop_pct or 0.0) / 100.0
        use_trailing = position.use_trailing_stop and trailing_pct > 0
        peak = position.peak_price or position.entry_price
        half_r = position.entry_price + abs(position.entry_price - position.stop_price) * 0.5 * (
            1 if position.side == "BUY" else -1
        )
        z_exit = float(self._config.get("intraday.vwap_fade_z_exit", 0.3) or 0.3)
        sigma_lb = int(self._config.get("intraday.vwap_fade_sigma_lookback", 15) or 15)

        for ts, row in future.iterrows():
            high = float(row["high"])
            low = float(row["low"])
            close = float(row["close"])
            if position.strategy == "vwap_fade" and position.entry_z is not None:
                window = frame.loc[frame.index <= ts]
                if len(window) >= sigma_lb + 1:
                    typical = (window["high"] + window["low"] + window["close"]) / 3.0
                    volume = window["volume"].clip(lower=0)
                    denom = volume.cumsum().replace(0, pd.NA)
                    vwap = (typical * volume).cumsum() / denom
                    residuals = window["close"] - vwap
                    sigma = float(residuals.tail(sigma_lb).std())
                    if sigma > 1e-9:
                        z_now = float((close - float(vwap.iloc[-1])) / sigma)
                        if abs(z_now) <= z_exit:
                            return {
                                "exit_time": ts.to_pydatetime(),
                                "exit_price": close,
                                "exit_reason": "vwap_z_exit",
                            }
            if position.side == "BUY":
                peak = max(peak, high)
                position.peak_price = peak
                if use_trailing and close >= half_r:
                    trail = peak * (1.0 - trailing_pct)
                    if low <= max(trail, position.stop_price):
                        return {
                            "exit_time": ts.to_pydatetime(),
                            "exit_price": max(trail, position.stop_price),
                            "exit_reason": "trailing_stop",
                        }
                if low <= position.stop_price:
                    return {
                        "exit_time": ts.to_pydatetime(),
                        "exit_price": position.stop_price,
                        "exit_reason": "stop_loss",
                    }
                if high >= position.target_price:
                    return {
                        "exit_time": ts.to_pydatetime(),
                        "exit_price": position.target_price,
                        "exit_reason": "target",
                    }
            else:
                peak = min(peak, low)
                position.peak_price = peak
                if use_trailing and close <= half_r:
                    trail = peak * (1.0 + trailing_pct)
                    if high >= min(trail, position.stop_price):
                        return {
                            "exit_time": ts.to_pydatetime(),
                            "exit_price": min(trail, position.stop_price),
                            "exit_reason": "trailing_stop",
                        }
                if high >= position.stop_price:
                    return {
                        "exit_time": ts.to_pydatetime(),
                        "exit_price": position.stop_price,
                        "exit_reason": "stop_loss",
                    }
                if low <= position.target_price:
                    return {
                        "exit_time": ts.to_pydatetime(),
                        "exit_price": position.target_price,
                        "exit_reason": "target",
                    }
        if force_exit:
            return {
                "exit_time": now,
                "exit_price": float(future["close"].iloc[-1]),
                "exit_reason": "force_squareoff",
            }
        if max_hold_exit:
            return {
                "exit_time": now,
                "exit_price": float(future["close"].iloc[-1]),
                "exit_reason": "max_hold",
            }
        return None

    def _trade_from_position(
        self,
        position: PaperPosition,
        exit_time: datetime,
        exit_price: float,
        exit_reason: str,
    ) -> IntradayTrade:
        slippage = float(self._config.get("intraday.slippage_bps_per_side", 2.0) or 2.0) / 10000.0
        paper_exit = exit_price * (
            1.0 - slippage if position.side == "BUY" else 1.0 + slippage
        )
        gross = (
            (paper_exit - position.entry_price) * position.quantity
            if position.side == "BUY"
            else (position.entry_price - paper_exit) * position.quantity
        )
        costs = self._costs(position.entry_price, paper_exit, position.quantity)
        net = gross - costs
        risk_amount = self._equity_from_completed() * float(
            self._config.get("intraday.risk_per_trade_pct", 0.4) or 0.4
        ) / 100.0
        return IntradayTrade(
            symbol=position.symbol,
            strategy=position.strategy,
            side=position.side,
            quantity=position.quantity,
            entry_time=position.entry_time,
            exit_time=exit_time,
            entry_price=position.entry_price,
            exit_price=round(paper_exit, 2),
            stop_price=position.stop_price,
            target_price=position.target_price,
            exit_reason=exit_reason,
            gross_pnl=round(gross, 2),
            costs=round(costs, 2),
            net_pnl=round(net, 2),
            r_multiple=round(net / risk_amount, 4) if risk_amount > 0 else 0.0,
        )

    def _costs(self, entry: float, exit: float, quantity: int) -> float:
        buy_value = entry * quantity
        sell_value = exit * quantity
        turnover = buy_value + sell_value
        brokerage_pct = float(
            self._config.get("intraday.costs.brokerage_pct", 0.03) or 0.03
        ) / 100.0
        cap = float(self._config.get("intraday.costs.brokerage_cap_per_order", 20.0) or 20.0)
        brokerage = min(cap, buy_value * brokerage_pct) + min(cap, sell_value * brokerage_pct)
        stt = sell_value * float(
            self._config.get("intraday.costs.stt_sell_pct", 0.025) or 0.025
        ) / 100.0
        exchange = turnover * float(
            self._config.get("intraday.costs.exchange_txn_pct", 0.00325) or 0.00325
        ) / 100.0
        sebi = turnover * float(
            self._config.get("intraday.costs.sebi_pct", 0.0001) or 0.0001
        ) / 100.0
        stamp = buy_value * float(
            self._config.get("intraday.costs.stamp_buy_pct", 0.003) or 0.003
        ) / 100.0
        gst = (brokerage + exchange + sebi) * float(
            self._config.get("intraday.costs.gst_pct", 18.0) or 18.0
        ) / 100.0
        slippage = turnover * float(
            self._config.get("intraday.slippage_bps_per_side", 2.0) or 2.0
        ) / 10000.0
        return brokerage + stt + exchange + sebi + stamp + gst + slippage

    def _estimate_costs(self, signal: IntradaySignal, state: PaperState) -> float:
        equity = self._equity(state)
        qty = int(
            min(
                equity
                * float(self._config.get("intraday.risk_per_trade_pct", 0.4) or 0.4)
                / 100.0
                / max(signal.risk_per_share, 0.01),
                equity
                * float(self._config.get("intraday.max_position_value_pct", 20.0) or 20.0)
                / 100.0
                / signal.entry_price,
            )
        )
        return self._costs(signal.entry_price, signal.target_price, max(qty, 1))

    @staticmethod
    def _watchlist_symbols(state: PaperState) -> Set[str]:
        symbols: Set[str] = set()
        for entry in state.watchlist:
            if not entry.get("in_play"):
                continue
            symbol = str(entry.get("symbol", "")).upper()
            if symbol:
                symbols.add(symbol)
        return symbols

    def _uses_watchlist_gate(self) -> bool:
        return self._config.persona_id not in ("spread", "xstat")

    def _poll_seconds(self, state: PaperState) -> int:
        base = max(int(self._config.get("intraday.watch_poll_seconds", 300) or 300), 1)
        if self._catalyst_scanned and state.watchlist:
            active = int(self._config.get("intraday.watch_active_poll_seconds", 0) or 0)
            if active > 0:
                return max(active, 1)
        return base

    def _bar_minutes(self) -> int:
        interval = str(self._config.get("intraday.default_interval", "FIVE_MINUTE")).upper()
        return {"ONE_MINUTE": 1, "FIVE_MINUTE": 5, "FIFTEEN_MINUTE": 15}.get(interval, 5)

    @staticmethod
    def _signal_key(signal: IntradaySignal) -> tuple:
        return (
            signal.symbol.upper(),
            signal.strategy,
            signal.side,
            signal.entry_time.isoformat(),
        )

    @staticmethod
    def _already_skipped(state: PaperState, signal: IntradaySignal) -> bool:
        key = IntradayWatchEngine._signal_key(signal)
        for row in state.skipped_signals:
            row_key = (
                str(row.get("symbol", "")).upper(),
                str(row.get("strategy", "")),
                str(row.get("side", "")),
                str(row.get("time", "")),
            )
            if row_key == key:
                return True
        return False

    def _is_stale_signal(
        self, signal: IntradaySignal, now: datetime, state: PaperState
    ) -> bool:
        poll = max(int(self._config.get("intraday.watch_poll_seconds", 300) or 300), 1)
        buffer_s = int(
            self._config.get("intraday.signal_stale_buffer_seconds", 120) or 120
        )
        max_age = timedelta(seconds=poll + buffer_s)
        # Age from bar close, not open — late watchlist discovery should not
        # instantly stale a signal that fired on the prior completed bar.
        signal_ts = _naive_exchange_dt(signal.entry_time, self._tz) + timedelta(
            minutes=self._bar_minutes()
        )
        now_ts = _naive_exchange_dt(now, self._tz)
        return now_ts - signal_ts > max_age

    def _can_enter_new_trades(self, state: PaperState, now: datetime) -> bool:
        if state.lockout_reason:
            return False
        if not state.active:
            return False
        if now.time() < parse_hhmm(str(self._config.get("intraday.trade_window_start", "09:45"))):
            return False
        if now.time() > parse_hhmm(str(self._config.get("intraday.trade_window_end", "14:30"))):
            return False
        return True

    def _apply_daily_lockout(self, state: PaperState) -> None:
        equity = float(self._config.secrets.initial_capital)
        max_loss = equity * float(
            self._config.get("intraday.max_daily_loss_pct", 1.5) or 1.5
        ) / 100.0
        if state.daily_pnl <= -max_loss:
            state.lockout_reason = f"max daily loss reached: Rs {state.daily_pnl:,.2f}"

    def _exchange_now(self, now: datetime) -> datetime:
        if now.tzinfo is None:
            return now.replace(tzinfo=self._tz)
        return now.astimezone(self._tz)

    def _force_exit_time(self) -> time:
        return parse_hhmm(str(self._config.get("intraday.force_exit_time", "15:15")))

    def _catalyst_scan_time(self) -> time:
        raw = self._config.get("intraday.catalyst_scan_time")
        if raw:
            return parse_hhmm(str(raw))
        open_t, _ = session_open_close(self._config)
        return time(open_t.hour, min(open_t.minute + 5, 59))

    def _is_market_watch_time(self, now: datetime) -> bool:
        now = self._exchange_now(now)
        if now.weekday() >= 5:
            return False
        open_t, _ = session_open_close(self._config)
        return open_t <= now.time() <= self._force_exit_time()

    def _equity(self, state: PaperState) -> float:
        base = max(float(self._config.secrets.initial_capital) + state.daily_pnl, 0.0)
        broker_exec = self._broker_executor()
        if broker_exec is not None:
            return broker_exec.equity(base)
        return base

    def _broker_executor(self) -> Optional[IntradayBrokerExecutor]:
        if not intraday_uses_broker(self._config):
            return None
        if self._broker_exec is None:
            self._broker_exec = IntradayBrokerExecutor(self._config)
        return self._broker_exec if self._broker_exec.active else None

    def _equity_from_completed(self) -> float:
        return float(self._config.secrets.initial_capital)

    @staticmethod
    def _skip(state: PaperState, signal: IntradaySignal, reason: str) -> None:
        row = {
            "symbol": signal.symbol,
            "strategy": signal.strategy,
            "side": signal.side,
            "time": signal.entry_time.isoformat(),
            "reason": reason,
        }
        if signal.symbol.upper() in state.style_profiles:
            row.update(state.style_profiles[signal.symbol.upper()])
        state.skipped_signals.append(row)

    def _save(self, state: PaperState) -> None:
        self._state_store.save(state)
        save_summary(self._config, summarize_state(self._config, state))

    def _emit_heartbeat(
        self,
        session_state: str,
        *,
        now: Optional[datetime] = None,
        state: Optional[PaperState] = None,
        force: bool = False,
    ) -> None:
        when = self._exchange_now(now or self._now())
        if (
            not force
            and self._last_heartbeat_at is not None
            and (when - self._last_heartbeat_at).total_seconds() < self._heartbeat_interval_seconds
        ):
            return
        self._last_heartbeat_at = when
        payload: Dict[str, Any] = {
            "persona_id": self._config.persona_id,
            "market_id": self._config.market_id,
            "session_state": session_state,
            "exchange_time": when.isoformat(timespec="seconds"),
            "heartbeat_interval_seconds": self._heartbeat_interval_seconds,
        }
        if state is not None:
            payload.update(
                {
                    "active": bool(state.active),
                    "open_positions": len(state.open_positions),
                    "completed_trades": len(state.completed_trades),
                    "daily_pnl": round(float(state.daily_pnl or 0.0), 2),
                    "last_scan_at": state.last_scan_at,
                }
            )
        log_structured_event(logger, "intraday_heartbeat", "heartbeat", **payload)
