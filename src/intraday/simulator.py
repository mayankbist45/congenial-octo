"""Cost-aware intraday paper simulator."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time
from typing import Dict, List, Optional

import pandas as pd

from src.config import Config
from src.intraday.agent import IntradayAgent
from src.intraday.costs import round_trip_costs
from src.intraday.engine_helpers import collect_signals
from src.intraday.kronos_service import maybe_enrich_ranked_with_kronos
from src.intraday.models import IntradayBacktestResult, IntradaySignal, IntradayTrade
from src.intraday.pair_exits import is_pair_strategy, pair_leg_quantities, resolve_pair_exit
from src.intraday.persona_rankers import filter_ranked_for_persona, load_ranker


class IntradaySimulator:
    def __init__(
        self,
        config: Config,
        agent: Optional[IntradayAgent] = None,
    ):
        self._config = config
        self._starting_capital = float(config.secrets.initial_capital)
        self._ranker = load_ranker(config)
        self._agent = agent or IntradayAgent(config)
        self._watchlist: Dict[str, dict] = {}

    def run(
        self,
        frames: Dict[str, pd.DataFrame],
        previous_days: Optional[Dict[str, Dict[str, float]]] = None,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
    ) -> IntradayBacktestResult:
        previous_days = previous_days or {}
        days = self._trading_days(frames)
        if start_date:
            days = [d for d in days if d >= start_date]
        if end_date:
            days = [d for d in days if d <= end_date]
        if not days:
            today = date.today()
            return IntradayBacktestResult(
                self._config.persona_id,
                start_date or today,
                end_date or today,
                self._starting_capital,
                self._starting_capital,
            )

        equity = self._starting_capital
        trades: List[IntradayTrade] = []
        equity_curve: List[tuple[date, float]] = []
        for day in days:
            day_trades = self._simulate_day(frames, previous_days, day, equity)
            daily_pnl = sum(t.net_pnl for t in day_trades)
            equity += daily_pnl
            trades.extend(day_trades)
            equity_curve.append((day, equity))

        strategy_pnl: Dict[str, float] = defaultdict(float)
        daily_pnl_map: Dict[str, float] = defaultdict(float)
        for trade in trades:
            strategy_pnl[trade.strategy] += trade.net_pnl
            daily_pnl_map[trade.trade_date.isoformat()] += trade.net_pnl

        return IntradayBacktestResult(
            persona_id=self._config.persona_id,
            start_date=days[0],
            end_date=days[-1],
            starting_capital=self._starting_capital,
            ending_capital=equity,
            trades=trades,
            equity_curve=equity_curve,
            strategy_pnl=dict(strategy_pnl),
            daily_pnl=dict(daily_pnl_map),
        )

    def _simulate_day(
        self,
        frames: Dict[str, pd.DataFrame],
        previous_days: Dict[str, Dict[str, float]],
        day: date,
        equity: float,
    ) -> List[IntradayTrade]:
        max_trades = int(self._config.get("intraday.max_trades_per_day", 3) or 3)
        max_daily_loss = equity * float(
            self._config.get("intraday.max_daily_loss_pct", 1.5) or 1.5
        ) / 100.0
        day_frames: Dict[str, pd.DataFrame] = {}
        day_previous: Dict[str, Dict[str, float]] = {}
        for symbol, frame in frames.items():
            day_frame = self._frame_for_day(frame, day)
            if not day_frame.empty:
                day_frames[symbol] = day_frame
            prior = frame.loc[frame.index.date < day]
            if not prior.empty:
                day_previous[symbol] = {
                    "close": float(prior["close"].iloc[-1]),
                    "high": float(prior["high"].iloc[-1]),
                    "low": float(prior["low"].iloc[-1]),
                }
            elif symbol in previous_days:
                day_previous[symbol] = previous_days[symbol]

        ranked = self._ranker.rank(day_frames, day_previous)
        limit = int(self._config.get("intraday.ranked_universe_size", 8) or 8)
        ranked = ranked[:limit]
        maybe_enrich_ranked_with_kronos(self._config, day_frames, ranked)
        pool = filter_ranked_for_persona(self._config, ranked)
        if not pool:
            pool = ranked

        scan_time = datetime.combine(day, time(9, 20))
        catalyst = self._agent.catalyst_scan(pool, day_frames, scan_time, 0.0)
        self._watchlist = {
            str(item["symbol"]).upper(): item
            for item in catalyst.get("watchlist", [])
            if item.get("in_play")
        }
        if self._watchlist and self._config.persona_id not in ("spread", "xstat"):
            ranked = [r for r in ranked if r.symbol in self._watchlist]

        collected = collect_signals(self._config, day_frames, ranked, day_previous)

        trades: List[IntradayTrade] = []
        daily_pnl = 0.0
        traded_keys: set[str] = set()
        for _, signal, item in collected:
            if len(trades) >= max_trades or daily_pnl <= -max_daily_loss:
                break
            trade_key = signal.pair_id or signal.symbol
            if trade_key in traded_keys:
                continue
            if self._is_short_disallowed(signal):
                continue
            frame = day_frames.get(signal.symbol)
            if frame is None:
                continue
            est_costs = self._estimate_costs(signal, equity)
            gate = self._agent.gate_trade(
                signal,
                item,
                frame,
                signal.entry_time,
                daily_pnl,
                self._watchlist.get(signal.symbol),
                est_costs,
            )
            if gate.get("decision") != "approve":
                continue
            if gate.get("adjusted_stop"):
                signal.stop_price = float(gate["adjusted_stop"])
            if gate.get("adjusted_target"):
                signal.target_price = float(gate["adjusted_target"])
            if gate.get("use_trailing_stop") is not None:
                signal.use_trailing_stop = bool(gate["use_trailing_stop"])
            day_trades = self._execute_signal(signal, day_frames, equity)
            for trade in day_trades:
                trades.append(trade)
                daily_pnl += trade.net_pnl
            if day_trades:
                traded_keys.add(trade_key)
        return trades

    def _execute_signal(
        self, signal: IntradaySignal, frames: Dict[str, pd.DataFrame], equity: float
    ) -> List[IntradayTrade]:
        if signal.is_pair and signal.pair_leg and is_pair_strategy(signal.strategy):
            return self._execute_pair(signal, frames, equity)
        trade = self._execute_leg(signal, frames.get(signal.symbol), equity)
        return [trade] if trade else []

    def _execute_pair(
        self, signal: IntradaySignal, frames: Dict[str, pd.DataFrame], equity: float
    ) -> List[IntradayTrade]:
        leg_b = signal.pair_leg
        if not leg_b:
            return []
        frame_a = frames.get(signal.symbol)
        frame_b = frames.get(leg_b)
        if frame_a is None or frame_b is None:
            return []
        entry_b = float(signal.pair_entry_price or 0.0)
        stop_b = float(signal.pair_stop_price or entry_b)
        beta = float(signal.pair_beta or 1.0)
        qty_a, qty_b = pair_leg_quantities(
            self._config,
            equity,
            signal.entry_price,
            signal.stop_price,
            entry_b,
            beta,
        )
        if qty_a <= 0 or qty_b <= 0:
            return []

        exit_time, exit_reason, _ = resolve_pair_exit(
            self._config,
            signal.strategy,
            frame_a,
            frame_b,
            signal.entry_time,
            signal.max_exit_time,
            beta=beta,
        )
        if exit_time is None or exit_reason is None:
            return []

        leg_b_signal = IntradaySignal(
            leg_b,
            signal.strategy,
            signal.pair_side or "BUY",
            signal.entry_time,
            entry_b,
            stop_b,
            float(signal.pair_target_price or entry_b),
            signal.reason,
            signal.max_exit_time,
            pair_id=signal.pair_id,
        )
        trades: List[IntradayTrade] = []
        for leg_signal, frame, qty in (
            (signal, frame_a, qty_a),
            (leg_b_signal, frame_b, qty_b),
        ):
            trade = self._leg_trade_at_exit(
                leg_signal, frame, qty, equity, exit_time, exit_reason
            )
            if trade:
                trade.pair_id = signal.pair_id
                trades.append(trade)
        return trades

    def _leg_trade_at_exit(
        self,
        signal: IntradaySignal,
        frame: pd.DataFrame,
        quantity: float,
        equity: float,
        exit_time: datetime,
        exit_reason: str,
    ) -> Optional[IntradayTrade]:
        exit_ts = pd.Timestamp(exit_time)
        if exit_ts in frame.index:
            exit_price = float(frame.loc[exit_ts, "close"])
        else:
            prior = frame.loc[frame.index <= exit_ts]
            if prior.empty:
                return None
            exit_price = float(prior["close"].iloc[-1])
        risk_amount = equity * float(
            self._config.get("intraday.risk_per_trade_pct", 0.4) or 0.4
        ) / 100.0
        gross = (
            (exit_price - signal.entry_price) * quantity
            if signal.side == "BUY"
            else (signal.entry_price - exit_price) * quantity
        )
        costs = self._costs(signal.entry_price, exit_price, quantity)
        net = gross - costs
        r_multiple = net / risk_amount if risk_amount > 0 else 0.0
        return IntradayTrade(
            symbol=signal.symbol,
            strategy=signal.strategy,
            side=signal.side,
            quantity=quantity,
            entry_time=signal.entry_time,
            exit_time=exit_time,
            entry_price=signal.entry_price,
            exit_price=exit_price,
            stop_price=signal.stop_price,
            target_price=signal.target_price,
            exit_reason=exit_reason,
            gross_pnl=round(gross, 2),
            costs=round(costs, 2),
            net_pnl=round(net, 2),
            r_multiple=round(r_multiple, 4),
            pair_id=signal.pair_id,
        )

    def _execute_leg(
        self, signal: IntradaySignal, frame: Optional[pd.DataFrame], equity: float
    ) -> Optional[IntradayTrade]:
        if frame is None or signal.entry_price <= 0 or signal.risk_per_share <= 0:
            return None
        risk_amount = equity * float(
            self._config.get("intraday.risk_per_trade_pct", 0.4) or 0.4
        ) / 100.0
        max_value = equity * float(
            self._config.get("intraday.max_position_value_pct", 20.0) or 20.0
        ) / 100.0
        quantity = self._size_quantity(
            min(risk_amount / signal.risk_per_share, max_value / signal.entry_price)
        )
        if quantity <= 0:
            return None

        entry_ts = pd.Timestamp(signal.entry_time)
        future = frame.loc[frame.index > entry_ts]
        if signal.max_exit_time:
            future = future.loc[future.index <= pd.Timestamp(signal.max_exit_time)]
        if future.empty:
            bar = frame.loc[frame.index >= entry_ts]
            if bar.empty:
                return None
            exit_time = bar.index[-1].to_pydatetime()
            exit_price = float(bar["close"].iloc[-1])
            gross = (
                (exit_price - signal.entry_price) * quantity
                if signal.side == "BUY"
                else (signal.entry_price - exit_price) * quantity
            )
            costs = self._costs(signal.entry_price, exit_price, quantity)
            net = gross - costs
            r_multiple = net / risk_amount if risk_amount > 0 else 0.0
            return IntradayTrade(
                symbol=signal.symbol,
                strategy=signal.strategy,
                side=signal.side,
                quantity=quantity,
                entry_time=signal.entry_time,
                exit_time=exit_time,
                entry_price=signal.entry_price,
                exit_price=exit_price,
                stop_price=signal.stop_price,
                target_price=signal.target_price,
                exit_reason="session_end",
                gross_pnl=gross,
                costs=costs,
                net_pnl=net,
                r_multiple=r_multiple,
            )

        exit_time = future.index[-1].to_pydatetime()
        exit_price = float(future["close"].iloc[-1])
        exit_reason = "time_exit"
        use_trailing = signal.use_trailing_stop
        trailing_pct = float(self._config.get("intraday.trailing_stop_pct", 0.25) or 0.25) / 100.0
        peak = signal.entry_price
        half_r = signal.entry_price + signal.risk_per_share * 0.5 * (
            1 if signal.side == "BUY" else -1
        )
        z_exit = float(self._config.get("intraday.vwap_fade_z_exit", 0.3) or 0.3)
        sigma_lb = int(self._config.get("intraday.vwap_fade_sigma_lookback", 15) or 15)

        for ts, row in future.iterrows():
            high = float(row["high"])
            low = float(row["low"])
            close = float(row["close"])
            if signal.strategy == "vwap_fade" and signal.entry_z is not None:
                window = frame.loc[frame.index <= ts]
                if len(window) >= sigma_lb + 1:
                    vwap_frame = window.copy()
                    typical = (vwap_frame["high"] + vwap_frame["low"] + vwap_frame["close"]) / 3.0
                    volume = vwap_frame["volume"].clip(lower=0)
                    denom = volume.cumsum().replace(0, pd.NA)
                    vwap_frame["vwap"] = (typical * volume).cumsum() / denom
                    residuals = vwap_frame["close"] - vwap_frame["vwap"]
                    sigma = float(residuals.tail(sigma_lb).std())
                    if sigma > 1e-9:
                        z_now = float((close - float(vwap_frame["vwap"].iloc[-1])) / sigma)
                        if abs(z_now) <= z_exit:
                            exit_time, exit_price, exit_reason = (
                                ts.to_pydatetime(),
                                close,
                                "vwap_z_exit",
                            )
                            break
            if signal.side == "BUY":
                peak = max(peak, high)
                if use_trailing and close >= half_r and peak > signal.entry_price:
                    trail = peak * (1.0 - trailing_pct)
                    if low <= max(trail, signal.stop_price):
                        exit_time, exit_price, exit_reason = (
                            ts.to_pydatetime(),
                            max(trail, signal.stop_price),
                            "trailing_stop",
                        )
                        break
                if low <= signal.stop_price:
                    exit_time, exit_price, exit_reason = (
                        ts.to_pydatetime(),
                        signal.stop_price,
                        "stop_loss",
                    )
                    break
                if high >= signal.target_price:
                    exit_time, exit_price, exit_reason = (
                        ts.to_pydatetime(),
                        signal.target_price,
                        "target",
                    )
                    break
            else:
                peak = min(peak, low)
                if use_trailing and close <= half_r and peak < signal.entry_price:
                    trail = peak * (1.0 + trailing_pct)
                    if high >= min(trail, signal.stop_price):
                        exit_time, exit_price, exit_reason = (
                            ts.to_pydatetime(),
                            min(trail, signal.stop_price),
                            "trailing_stop",
                        )
                        break
                if high >= signal.stop_price:
                    exit_time, exit_price, exit_reason = (
                        ts.to_pydatetime(),
                        signal.stop_price,
                        "stop_loss",
                    )
                    break
                if low <= signal.target_price:
                    exit_time, exit_price, exit_reason = (
                        ts.to_pydatetime(),
                        signal.target_price,
                        "target",
                    )
                    break

        gross = (
            (exit_price - signal.entry_price) * quantity
            if signal.side == "BUY"
            else (signal.entry_price - exit_price) * quantity
        )
        costs = self._costs(signal.entry_price, exit_price, quantity)
        net = gross - costs
        r_multiple = net / risk_amount if risk_amount > 0 else 0.0
        return IntradayTrade(
            symbol=signal.symbol,
            strategy=signal.strategy,
            side=signal.side,
            quantity=quantity,
            entry_time=signal.entry_time,
            exit_time=exit_time,
            entry_price=signal.entry_price,
            exit_price=exit_price,
            stop_price=signal.stop_price,
            target_price=signal.target_price,
            exit_reason=exit_reason,
            gross_pnl=round(gross, 2),
            costs=round(costs, 2),
            net_pnl=round(net, 2),
            r_multiple=round(r_multiple, 4),
        )

    def _estimate_costs(self, signal: IntradaySignal, equity: float) -> float:
        qty = self._position_quantity_for_signal(signal, equity)
        if qty <= 0:
            return 0.0
        costs = self._costs(signal.entry_price, signal.target_price, qty)
        if signal.is_pair and signal.pair_entry_price and signal.pair_target_price:
            qty_b = qty
            costs += self._costs(
                float(signal.pair_entry_price),
                float(signal.pair_target_price),
                qty_b,
            )
        return costs

    def _costs(self, entry: float, exit: float, quantity: float) -> float:
        return round_trip_costs(self._config, entry, exit, quantity)

    def _position_quantity_for_signal(self, signal: IntradaySignal, equity: float) -> float:
        raw_quantity = min(
            equity
            * float(self._config.get("intraday.risk_per_trade_pct", 0.4) or 0.4)
            / 100.0
            / max(signal.risk_per_share, 0.01),
            equity
            * float(self._config.get("intraday.max_position_value_pct", 20.0) or 20.0)
            / 100.0
            / max(signal.entry_price, 0.01),
        )
        return self._size_quantity(raw_quantity)

    def _size_quantity(self, raw_quantity: float) -> float:
        if raw_quantity <= 0:
            return 0.0
        if str(self._config.market.asset_class).lower() == "crypto":
            step = float(self._config.get("intraday.quantity_step", 0.0001) or 0.0001)
            if step <= 0:
                step = 0.0001
            scaled = int(raw_quantity / step)
            qty = scaled * step
            return round(
                qty, int(self._config.get("intraday.quantity_decimals", 4) or 4)
            )
        return float(int(raw_quantity))

    def _is_short_disallowed(self, signal: IntradaySignal) -> bool:
        if str(self._config.market.asset_class).lower() != "crypto":
            return False
        return signal.side.upper() == "SELL" and not bool(
            self._config.get("intraday.allow_short", False)
        )

    @staticmethod
    def _trading_days(frames: Dict[str, pd.DataFrame]) -> List[date]:
        days = set()
        for frame in frames.values():
            for ts in frame.index:
                days.add(ts.date())
        return sorted(days)

    @staticmethod
    def _frame_for_day(frame: pd.DataFrame, day: date) -> pd.DataFrame:
        if frame.empty:
            return frame
        return frame.loc[frame.index.date == day]
