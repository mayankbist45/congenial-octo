"""Live market-hours paper trading loop for Calan."""

from __future__ import annotations

import time as time_module
from dataclasses import asdict
from datetime import date, datetime, time
from typing import Callable, Dict, Iterable, List, Optional

import pandas as pd

from src.config import Config
from src.intraday.data import IntradayDataService
from src.intraday.models import IntradaySignal, IntradayTrade, PaperPosition, PaperState, RankedStock
from src.intraday.ranking import IntradayRanker
from src.intraday.reporting import save_summary, summarize_state
from src.intraday.state import PaperStateStore
from src.intraday.strategies import DEFAULT_STRATEGIES


def parse_hhmm(value: str) -> time:
    hour, minute = str(value).split(":", 1)
    return time(int(hour), int(minute))


class IntradayWatchEngine:
    """Refresh live candles and manage paper orders for one intraday persona."""

    def __init__(
        self,
        config: Config,
        data_service: IntradayDataService,
        symbols: Iterable[str],
        state_store: Optional[PaperStateStore] = None,
        now: Optional[Callable[[], datetime]] = None,
        sleep: Optional[Callable[[float], None]] = None,
    ):
        self._config = config
        self._data = data_service
        self._symbols = list(symbols)
        self._state_store = state_store or PaperStateStore(config)
        self._now = now or datetime.now
        self._sleep = sleep or time_module.sleep
        self._ranker = IntradayRanker(config)
        self._strategies = DEFAULT_STRATEGIES

    def run_until_close(self) -> PaperState:
        """Run every configured cadence until square-off time."""
        poll_seconds = int(self._config.get("intraday.watch_poll_seconds", 300) or 300)
        while True:
            now = self._now()
            state = self.run_once(now)
            if now.time() >= parse_hhmm(str(self._config.get("intraday.force_exit_time", "15:15"))):
                state.active = False
                self._save(state)
                return state
            self._sleep(max(poll_seconds, 1))

    def run_once(self, now: Optional[datetime] = None) -> PaperState:
        now = now or self._now()
        state = self._state_store.load(now.date())
        state.active = self._is_market_watch_time(now)
        state.last_scan_at = now.isoformat(timespec="seconds")

        symbols = self._symbols_for_poll(state, now)
        frames = self._load_frames(now, symbols)
        self._close_positions(state, frames, now)
        ranked = self._ranker.rank(frames)
        limit = int(self._config.get("intraday.ranked_universe_size", 40) or 40)
        ranked = ranked[:limit]
        state.top_candidates = [asdict(item) for item in ranked]

        if self._can_enter_new_trades(state, now):
            self._open_new_positions(state, frames, ranked, now)

        self._apply_daily_lockout(state)
        self._save(state)
        return state

    def _symbols_for_poll(self, state: PaperState, now: datetime) -> List[str]:
        """Open positions always; scan a small rotating slice of the universe.

        Angel One historical candles rate-limit hard, so a full Nifty 50 pull
        every poll is still too much after a burst. Rotate through the list so
        coverage stays broad across the session without hammering one poll.
        """
        scan_size = int(self._config.get("intraday.watch_scan_size", 12) or 12)
        must = [p.symbol for p in state.open_positions]
        if not self._symbols:
            return list(dict.fromkeys(must))

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
                    symbol, now.date(), interval=interval, use_cache=False
                )
            except Exception:
                continue
            if not frame.empty:
                frames[symbol] = frame.loc[frame.index <= pd.Timestamp(now)]
        return frames

    def _open_new_positions(
        self,
        state: PaperState,
        frames: Dict[str, pd.DataFrame],
        ranked: List[RankedStock],
        now: datetime,
    ) -> None:
        signals = self._rank_signals(frames, ranked, state)
        max_trades = int(self._config.get("intraday.max_trades_per_day", 5) or 5)
        max_open = int(self._config.get("intraday.max_open_positions", 2) or 2)
        for _, signal in signals:
            if state.trade_count >= max_trades:
                self._skip(state, signal, "max trades reached")
                continue
            if len(state.open_positions) >= max_open:
                self._skip(state, signal, "max open positions reached")
                continue
            if any(p.symbol == signal.symbol for p in state.open_positions):
                self._skip(state, signal, "symbol already has an open paper position")
                continue
            if any(t.symbol == signal.symbol for t in state.completed_trades):
                self._skip(state, signal, "symbol already traded today")
                continue
            position = self._paper_fill(signal, state)
            if position is None:
                self._skip(state, signal, "position size rounded to zero")
                continue
            state.open_positions.append(position)

    def _rank_signals(
        self,
        frames: Dict[str, pd.DataFrame],
        ranked: List[RankedStock],
        state: PaperState,
    ) -> List[tuple[float, IntradaySignal]]:
        by_symbol = {item.symbol: item for item in ranked}
        out: List[tuple[float, IntradaySignal]] = []
        for item in ranked:
            frame = frames.get(item.symbol)
            if frame is None or frame.empty:
                continue
            best: Optional[tuple[float, IntradaySignal]] = None
            for strategy in self._strategies:
                for signal in strategy.signals(item.symbol, frame, self._config):
                    score = self._signal_score(signal, item, frame)
                    if best is None or score > best[0]:
                        best = (score, signal)
            if best:
                out.append(best)
            elif item.symbol not in by_symbol:
                state.skipped_signals.append({"symbol": item.symbol, "reason": "no valid strategy signal"})
        out.sort(key=lambda row: row[0], reverse=True)
        return out

    def _signal_score(self, signal: IntradaySignal, ranked: RankedStock, frame: pd.DataFrame) -> float:
        risk = signal.risk_per_share
        reward = abs(signal.target_price - signal.entry_price)
        expected_r = reward / risk if risk > 0 else 0.0
        latest_volume = float(frame["volume"].iloc[-1])
        avg_volume = float(frame["volume"].tail(12).mean())
        volume_confirm = latest_volume / avg_volume if avg_volume > 0 else 1.0
        strategy_priority = {
            "opening_range_breakout": 4.0,
            "vwap_pullback": 3.0,
            "previous_day_break": 2.0,
            "scalping": 1.0,
        }.get(signal.strategy, 0.0)
        scalp_penalty = 0.0
        if signal.strategy == "scalping":
            target_pct = abs(signal.target_price / signal.entry_price - 1.0) * 100.0
            min_edge = float(self._config.get("intraday.scalping_min_net_edge_bps", 8.0) or 8.0) / 100.0
            scalp_penalty = 20.0 if target_pct < min_edge else 0.0
        return (
            ranked.score
            + expected_r * 18.0
            + min(volume_confirm, 4.0) * 5.0
            + strategy_priority
            - scalp_penalty
        )

    def _paper_fill(self, signal: IntradaySignal, state: PaperState) -> Optional[PaperPosition]:
        equity = self._equity(state)
        risk_amount = equity * float(self._config.get("intraday.risk_per_trade_pct", 0.4) or 0.4) / 100.0
        max_value = equity * float(self._config.get("intraday.max_position_value_pct", 35.0) or 35.0) / 100.0
        slippage = float(self._config.get("intraday.slippage_bps_per_side", 2.0) or 2.0) / 10000.0
        entry_price = signal.entry_price * (1.0 + slippage if signal.side == "BUY" else 1.0 - slippage)
        risk_per_share = abs(entry_price - signal.stop_price)
        if entry_price <= 0 or risk_per_share <= 0:
            return None
        quantity = int(min(risk_amount / risk_per_share, max_value / entry_price))
        if quantity < 1:
            return None
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
        )

    def _close_positions(
        self, state: PaperState, frames: Dict[str, pd.DataFrame], now: datetime
    ) -> None:
        still_open: List[PaperPosition] = []
        for position in state.open_positions:
            frame = frames.get(position.symbol)
            exit_payload = self._exit_for_position(position, frame, now)
            if exit_payload is None:
                still_open.append(position)
                continue
            trade = self._trade_from_position(position, **exit_payload)
            state.completed_trades.append(trade)
            state.daily_pnl = round(state.daily_pnl + trade.net_pnl, 2)
        state.open_positions = still_open

    def _exit_for_position(
        self,
        position: PaperPosition,
        frame: Optional[pd.DataFrame],
        now: datetime,
    ) -> Optional[Dict[str, object]]:
        force_exit = now.time() >= parse_hhmm(str(self._config.get("intraday.force_exit_time", "15:15")))
        max_hold_exit = position.max_exit_time is not None and now >= position.max_exit_time
        if frame is None or frame.empty:
            if force_exit or max_hold_exit:
                return {"exit_time": now, "exit_price": position.entry_price, "exit_reason": "paper_time_exit_no_candle"}
            return None
        future = frame.loc[frame.index >= pd.Timestamp(position.entry_time)]
        if future.empty:
            return None
        if not force_exit:
            future = future.loc[future.index <= pd.Timestamp(now)]
        if future.empty:
            return None
        for ts, row in future.iterrows():
            high = float(row["high"])
            low = float(row["low"])
            if position.side == "BUY":
                if low <= position.stop_price:
                    return {"exit_time": ts.to_pydatetime(), "exit_price": position.stop_price, "exit_reason": "stop_loss"}
                if high >= position.target_price:
                    return {"exit_time": ts.to_pydatetime(), "exit_price": position.target_price, "exit_reason": "target"}
            else:
                if high >= position.stop_price:
                    return {"exit_time": ts.to_pydatetime(), "exit_price": position.stop_price, "exit_reason": "stop_loss"}
                if low <= position.target_price:
                    return {"exit_time": ts.to_pydatetime(), "exit_price": position.target_price, "exit_reason": "target"}
        if force_exit:
            return {"exit_time": now, "exit_price": float(future["close"].iloc[-1]), "exit_reason": "force_squareoff"}
        if max_hold_exit:
            return {"exit_time": now, "exit_price": float(future["close"].iloc[-1]), "exit_reason": "max_hold"}
        return None

    def _trade_from_position(
        self,
        position: PaperPosition,
        exit_time: datetime,
        exit_price: float,
        exit_reason: str,
    ) -> IntradayTrade:
        slippage = float(self._config.get("intraday.slippage_bps_per_side", 2.0) or 2.0) / 10000.0
        paper_exit = exit_price * (1.0 - slippage if position.side == "BUY" else 1.0 + slippage)
        gross = (
            (paper_exit - position.entry_price) * position.quantity
            if position.side == "BUY"
            else (position.entry_price - paper_exit) * position.quantity
        )
        costs = self._costs(position.entry_price, paper_exit, position.quantity)
        net = gross - costs
        risk_amount = self._equity_from_completed() * float(self._config.get("intraday.risk_per_trade_pct", 0.4) or 0.4) / 100.0
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

    def _can_enter_new_trades(self, state: PaperState, now: datetime) -> bool:
        if state.lockout_reason:
            return False
        if not state.active:
            return False
        if now.time() < parse_hhmm(str(self._config.get("intraday.trade_window_start", "09:30"))):
            return False
        if now.time() > parse_hhmm(str(self._config.get("intraday.trade_window_end", "14:45"))):
            return False
        return True

    def _apply_daily_lockout(self, state: PaperState) -> None:
        equity = float(self._config.secrets.initial_capital)
        max_loss = equity * float(self._config.get("intraday.max_daily_loss_pct", 1.5) or 1.5) / 100.0
        if state.daily_pnl <= -max_loss:
            state.lockout_reason = f"max daily loss reached: Rs {state.daily_pnl:,.2f}"

    def _is_market_watch_time(self, now: datetime) -> bool:
        return now.time() <= parse_hhmm(str(self._config.get("intraday.force_exit_time", "15:15")))

    def _equity(self, state: PaperState) -> float:
        return max(float(self._config.secrets.initial_capital) + state.daily_pnl, 0.0)

    def _equity_from_completed(self) -> float:
        return float(self._config.secrets.initial_capital)

    @staticmethod
    def _skip(state: PaperState, signal: IntradaySignal, reason: str) -> None:
        state.skipped_signals.append(
            {
                "symbol": signal.symbol,
                "strategy": signal.strategy,
                "side": signal.side,
                "time": signal.entry_time.isoformat(),
                "reason": reason,
            }
        )

    def _save(self, state: PaperState) -> None:
        self._state_store.save(state)
        save_summary(self._config, summarize_state(self._config, state))
