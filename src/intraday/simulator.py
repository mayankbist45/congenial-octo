"""Cost-aware intraday paper simulator."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time
from typing import Dict, List, Optional

import pandas as pd

from src.config import Config
from src.intraday.agent import IntradayAgent
from src.intraday.engine_helpers import collect_signals
from src.intraday.models import IntradayBacktestResult, IntradaySignal, IntradayTrade
from src.intraday.cross_stat_math import is_two_leg_persona
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
        if self._watchlist and not is_two_leg_persona(self._config):
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
        trades: List[IntradayTrade] = []
        primary = self._execute_leg(signal, frames.get(signal.symbol), equity)
        if primary:
            trades.append(primary)
        if signal.is_pair and signal.pair_leg:
            leg = IntradaySignal(
                signal.pair_leg,
                signal.strategy,
                signal.pair_side or "BUY",
                signal.entry_time,
                float(signal.pair_entry_price or 0.0),
                float(signal.pair_stop_price or 0.0),
                float(signal.pair_target_price or 0.0),
                signal.reason,
                signal.max_exit_time,
            )
            secondary = self._execute_leg(leg, frames.get(signal.pair_leg), equity)
            if secondary:
                trades.append(secondary)
        return trades

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
        quantity = int(min(risk_amount / signal.risk_per_share, max_value / signal.entry_price))
        if quantity < 1:
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
        trailing_pct = float(self._config.get("intraday.trailing_stop_pct", 0.25) or 0.25) / 100.0
        peak = signal.entry_price
        half_r = signal.entry_price + signal.risk_per_share * 0.5 * (
            1 if signal.side == "BUY" else -1
        )

        for ts, row in future.iterrows():
            high = float(row["high"])
            low = float(row["low"])
            close = float(row["close"])
            if signal.side == "BUY":
                peak = max(peak, high)
                if close >= half_r and peak > signal.entry_price:
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
                if close <= half_r and peak < signal.entry_price:
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
        costs = self._costs(signal.entry_price, signal.target_price, max(qty, 1))
        if signal.is_pair and signal.pair_entry_price and signal.pair_target_price:
            qty_b = max(qty, 1)
            costs += self._costs(
                float(signal.pair_entry_price),
                float(signal.pair_target_price),
                qty_b,
            )
        return costs

    def _costs(self, entry: float, exit: float, quantity: int) -> float:
        buy_value = entry * quantity
        sell_value = exit * quantity
        turnover = buy_value + sell_value
        brokerage_pct = float(
            self._config.get("intraday.costs.brokerage_pct", 0.03) or 0.03
        ) / 100.0
        cap = float(self._config.get("intraday.costs.brokerage_cap_per_order", 20.0) or 20.0)
        brokerage = min(cap, buy_value * brokerage_pct) + min(cap, sell_value * brokerage_pct)
        stt = sell_value * float(self._config.get("intraday.costs.stt_sell_pct", 0.025) or 0.025) / 100.0
        exchange = turnover * float(
            self._config.get("intraday.costs.exchange_txn_pct", 0.00325) or 0.00325
        ) / 100.0
        sebi = turnover * float(self._config.get("intraday.costs.sebi_pct", 0.0001) or 0.0001) / 100.0
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
