"""Cost-aware intraday paper trading simulator."""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Dict, List, Optional

import pandas as pd

from src.config import Config
from src.intraday.models import IntradayBacktestResult, IntradaySignal, IntradayTrade
from src.intraday.ranking import IntradayRanker
from src.intraday.strategies import DEFAULT_STRATEGIES


class IntradaySimulator:
    def __init__(self, config: Config, strategies: Optional[list] = None):
        self._config = config
        self._strategies = strategies or DEFAULT_STRATEGIES
        self._starting_capital = float(config.secrets.initial_capital)
        self._ranker = IntradayRanker(config)

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
            return IntradayBacktestResult(self._config.persona_id, start_date or today, end_date or today, self._starting_capital, self._starting_capital)

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
        max_trades = int(self._config.get("intraday.max_trades_per_day", 5) or 5)
        max_daily_loss = equity * float(self._config.get("intraday.max_daily_loss_pct", 1.5) or 1.5) / 100.0
        day_frames: Dict[str, pd.DataFrame] = {}
        for symbol, frame in frames.items():
            day_frame = self._frame_for_day(frame, day)
            if day_frame.empty:
                continue
            day_frames[symbol] = day_frame

        ranked = self._ranker.rank(day_frames, previous_days)
        limit = int(self._config.get("intraday.ranked_universe_size", 40) or 40)
        ranked_symbols = {item.symbol for item in ranked[:limit]}
        rank_score = {item.symbol: item.score for item in ranked[:limit]}

        signals: List[tuple[float, IntradaySignal]] = []
        for symbol, day_frame in day_frames.items():
            if symbol not in ranked_symbols:
                continue
            for strategy in self._strategies:
                for signal in strategy.signals(symbol, day_frame, self._config, previous_days.get(symbol)):
                    signals.append((self._signal_score(signal, rank_score.get(symbol, 0.0), day_frame), signal))
        signals.sort(key=lambda row: (-row[0], row[1].entry_time))

        trades: List[IntradayTrade] = []
        daily_pnl = 0.0
        traded_symbols: set[str] = set()
        max_open = int(self._config.get("intraday.max_open_positions", 2) or 2)
        for _, signal in signals:
            if len(trades) >= max_trades or daily_pnl <= -max_daily_loss:
                break
            if signal.symbol in traded_symbols:
                continue
            open_count = sum(
                1 for trade in trades
                if trade.entry_time <= signal.entry_time < trade.exit_time
            )
            if open_count >= max_open:
                continue
            trade = self._execute_signal(signal, day_frames[signal.symbol], equity)
            if trade is None:
                continue
            trades.append(trade)
            traded_symbols.add(signal.symbol)
            daily_pnl += trade.net_pnl
        return trades

    def _signal_score(
        self, signal: IntradaySignal, stock_score: float, frame: pd.DataFrame
    ) -> float:
        risk = signal.risk_per_share
        reward = abs(signal.target_price - signal.entry_price)
        expected_r = reward / risk if risk > 0 else 0.0
        latest_volume = float(frame["volume"].iloc[-1])
        avg_volume = float(frame["volume"].tail(12).mean())
        volume_confirm = latest_volume / avg_volume if avg_volume > 0 else 1.0
        priority = {
            "opening_range_breakout": 4.0,
            "vwap_pullback": 3.0,
            "previous_day_break": 2.0,
            "scalping": 1.0,
        }.get(signal.strategy, 0.0)
        return stock_score + expected_r * 18.0 + min(volume_confirm, 4.0) * 5.0 + priority

    def _execute_signal(
        self, signal: IntradaySignal, frame: pd.DataFrame, equity: float
    ) -> Optional[IntradayTrade]:
        if signal.entry_price <= 0 or signal.risk_per_share <= 0:
            return None
        risk_amount = equity * float(self._config.get("intraday.risk_per_trade_pct", 0.4) or 0.4) / 100.0
        max_value = equity * float(self._config.get("intraday.max_position_value_pct", 35.0) or 35.0) / 100.0
        quantity = int(min(risk_amount / signal.risk_per_share, max_value / signal.entry_price))
        if quantity < 1:
            return None

        future = frame.loc[frame.index >= pd.Timestamp(signal.entry_time)]
        if signal.max_exit_time:
            future = future.loc[future.index <= pd.Timestamp(signal.max_exit_time)]
        if future.empty:
            return None

        exit_time = future.index[-1].to_pydatetime()
        exit_price = float(future["close"].iloc[-1])
        exit_reason = "time_exit"
        for ts, row in future.iterrows():
            high = float(row["high"])
            low = float(row["low"])
            if signal.side == "BUY":
                if low <= signal.stop_price:
                    exit_time, exit_price, exit_reason = ts.to_pydatetime(), signal.stop_price, "stop_loss"
                    break
                if high >= signal.target_price:
                    exit_time, exit_price, exit_reason = ts.to_pydatetime(), signal.target_price, "target"
                    break
            else:
                if high >= signal.stop_price:
                    exit_time, exit_price, exit_reason = ts.to_pydatetime(), signal.stop_price, "stop_loss"
                    break
                if low <= signal.target_price:
                    exit_time, exit_price, exit_reason = ts.to_pydatetime(), signal.target_price, "target"
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

    def _costs(self, entry: float, exit: float, quantity: int) -> float:
        buy_value = entry * quantity
        sell_value = exit * quantity
        turnover = buy_value + sell_value
        brokerage_pct = float(self._config.get("intraday.costs.brokerage_pct", 0.03) or 0.03) / 100.0
        cap = float(self._config.get("intraday.costs.brokerage_cap_per_order", 20.0) or 20.0)
        brokerage = min(cap, buy_value * brokerage_pct) + min(cap, sell_value * brokerage_pct)
        stt = sell_value * float(self._config.get("intraday.costs.stt_sell_pct", 0.025) or 0.025) / 100.0
        exchange = turnover * float(self._config.get("intraday.costs.exchange_txn_pct", 0.00325) or 0.00325) / 100.0
        sebi = turnover * float(self._config.get("intraday.costs.sebi_pct", 0.0001) or 0.0001) / 100.0
        stamp = buy_value * float(self._config.get("intraday.costs.stamp_buy_pct", 0.003) or 0.003) / 100.0
        gst = (brokerage + exchange + sebi) * float(self._config.get("intraday.costs.gst_pct", 18.0) or 18.0) / 100.0
        slippage = turnover * float(self._config.get("intraday.slippage_bps_per_side", 2.0) or 2.0) / 10000.0
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
