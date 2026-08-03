"""Summaries and persistence for intraday runs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from src.config import Config
from src.intraday.models import IntradayBacktestResult, PaperState


def summarize_result(result: IntradayBacktestResult) -> Dict[str, Any]:
    wins = [t for t in result.trades if t.net_pnl > 0]
    total = len(result.trades)
    win_rate = (len(wins) / total * 100.0) if total else 0.0
    expectancy = (
        sum(t.r_multiple for t in result.trades) / total if total else 0.0
    )
    gross_wins = sum(t.net_pnl for t in wins)
    gross_losses = abs(sum(t.net_pnl for t in result.trades if t.net_pnl <= 0))
    profit_factor = (gross_wins / gross_losses) if gross_losses > 0 else None
    return {
        "persona_id": result.persona_id,
        "start_date": result.start_date.isoformat(),
        "end_date": result.end_date.isoformat(),
        "starting_capital": result.starting_capital,
        "ending_capital": result.ending_capital,
        "total_net_pnl": round(result.total_net_pnl, 2),
        "total_return_pct": round(result.total_return_pct, 2),
        "total_trades": total,
        "win_rate_pct": round(win_rate, 1),
        "expectancy_r": round(expectancy, 3),
        "profit_factor": round(profit_factor, 3) if profit_factor is not None else None,
        "strategy_pnl": {k: round(v, 2) for k, v in result.strategy_pnl.items()},
        "daily_pnl": {k: round(v, 2) for k, v in result.daily_pnl.items()},
        "trades": [_trade_dict(t) for t in result.trades],
    }


def summarize_state(config: Config, state: PaperState) -> Dict[str, Any]:
    trades = state.completed_trades
    wins = [t for t in trades if t.net_pnl > 0]
    total = len(trades)
    win_rate = (len(wins) / total * 100.0) if total else 0.0
    expectancy = sum(t.r_multiple for t in trades) / total if total else 0.0
    capital = float(config.secrets.initial_capital)
    return {
        "persona_id": state.persona_id,
        "start_date": state.trading_date.isoformat(),
        "end_date": state.trading_date.isoformat(),
        "starting_capital": capital,
        "ending_capital": round(capital + state.daily_pnl, 2),
        "total_net_pnl": round(state.daily_pnl, 2),
        "total_return_pct": round(state.daily_pnl / capital * 100.0, 2) if capital else 0.0,
        "total_trades": total,
        "win_rate_pct": round(win_rate, 1),
        "expectancy_r": round(expectancy, 3),
        "daily_net_pnl": round(state.daily_pnl, 2),
        "label": "live paper",
        "active": state.active,
        "last_scan_at": state.last_scan_at,
        "lockout_reason": state.lockout_reason,
        "watchlist": state.watchlist,
        "open_positions": [_position_dict(p) for p in state.open_positions],
        "completed_trades": [_trade_dict(t) for t in trades],
        "skipped_signals": state.skipped_signals,
        "top_candidates": state.top_candidates,
        "state_path": str(config.intraday_dir / f"{config.persona_id}_state.json"),
    }


def save_summary(config: Config, summary: Dict[str, Any]) -> Path:
    path = config.intraday_dir / f"{config.persona_id}_latest.json"
    path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return path


def _trade_dict(trade) -> dict:
    return {
        "symbol": trade.symbol,
        "strategy": trade.strategy,
        "side": trade.side,
        "quantity": trade.quantity,
        "entry_time": trade.entry_time.strftime("%Y-%m-%d %H:%M:%S"),
        "exit_time": trade.exit_time.strftime("%Y-%m-%d %H:%M:%S"),
        "entry_price": trade.entry_price,
        "exit_price": trade.exit_price,
        "stop_price": trade.stop_price,
        "target_price": trade.target_price,
        "exit_reason": trade.exit_reason,
        "gross_pnl": trade.gross_pnl,
        "costs": trade.costs,
        "net_pnl": trade.net_pnl,
        "r_multiple": trade.r_multiple,
    }


def _position_dict(position) -> dict:
    return {
        "symbol": position.symbol,
        "strategy": position.strategy,
        "side": position.side,
        "quantity": position.quantity,
        "entry_time": position.entry_time.isoformat(sep=" "),
        "entry_price": position.entry_price,
        "stop_price": position.stop_price,
        "target_price": position.target_price,
        "reason": position.reason,
    }
