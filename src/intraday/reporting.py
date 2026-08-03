"""Reporting helpers for intraday backtests."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from statistics import median
from typing import Any, Dict

from src.config import Config
from src.intraday.models import IntradayBacktestResult, PaperState
from src.intraday.state import PaperStateStore, trade_to_dict


def summarize_result(result: IntradayBacktestResult) -> Dict[str, Any]:
    trades = result.trades
    wins = [t for t in trades if t.net_pnl > 0]
    losses = [t for t in trades if t.net_pnl < 0]
    gross_profit = sum(t.net_pnl for t in wins)
    gross_loss = abs(sum(t.net_pnl for t in losses))
    avg_win_r = sum(t.r_multiple for t in wins) / len(wins) if wins else 0.0
    avg_loss_r = abs(sum(t.r_multiple for t in losses) / len(losses)) if losses else 0.0
    win_rate = len(wins) / len(trades) * 100.0 if trades else 0.0
    expectancy_r = (win_rate / 100.0 * avg_win_r) - ((100.0 - win_rate) / 100.0 * avg_loss_r)
    daily_values = list(result.daily_pnl.values())

    peak = result.starting_capital
    max_drawdown = 0.0
    for _, equity in result.equity_curve:
        peak = max(peak, equity)
        if peak > 0:
            max_drawdown = min(max_drawdown, (equity / peak - 1.0) * 100.0)

    losing_streak = 0
    max_losing_streak = 0
    for trade in trades:
        if trade.net_pnl < 0:
            losing_streak += 1
            max_losing_streak = max(max_losing_streak, losing_streak)
        else:
            losing_streak = 0

    return {
        "persona_id": result.persona_id,
        "start_date": result.start_date.isoformat(),
        "end_date": result.end_date.isoformat(),
        "starting_capital": result.starting_capital,
        "ending_capital": result.ending_capital,
        "total_net_pnl": round(result.total_net_pnl, 2),
        "total_return_pct": round(result.total_return_pct, 2),
        "total_trades": len(trades),
        "win_rate_pct": round(win_rate, 2),
        "expectancy_r": round(expectancy_r, 4),
        "profit_factor": round(gross_profit / gross_loss, 3) if gross_loss > 0 else None,
        "max_drawdown_pct": round(max_drawdown, 2),
        "best_day_pnl": round(max(daily_values), 2) if daily_values else 0.0,
        "worst_day_pnl": round(min(daily_values), 2) if daily_values else 0.0,
        "median_day_pnl": round(median(daily_values), 2) if daily_values else 0.0,
        "max_losing_streak": max_losing_streak,
        "strategy_pnl": {k: round(v, 2) for k, v in result.strategy_pnl.items()},
        "daily_pnl": {k: round(v, 2) for k, v in result.daily_pnl.items()},
        "trades": [asdict(t) for t in trades],
        "label": "paper/backtest",
    }


def save_summary(config: Config, summary: Dict[str, Any]) -> Path:
    path = config.root / "data" / "intraday" / f"{config.persona_id}_latest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    return path


def summarize_state(config: Config, state: PaperState) -> Dict[str, Any]:
    trades = state.completed_trades
    starting = float(config.secrets.initial_capital)
    ending = starting + state.daily_pnl
    result = IntradayBacktestResult(
        persona_id=config.persona_id,
        start_date=date_from_iso(state.trading_date),
        end_date=date_from_iso(state.trading_date),
        starting_capital=starting,
        ending_capital=ending,
        trades=trades,
        equity_curve=[(date_from_iso(state.trading_date), ending)],
    )
    summary = summarize_result(result)
    strategy_pnl: Dict[str, float] = {}
    for trade in trades:
        strategy_pnl[trade.strategy] = strategy_pnl.get(trade.strategy, 0.0) + trade.net_pnl
    summary["strategy_pnl"] = {k: round(v, 2) for k, v in strategy_pnl.items()}
    summary.update(
        {
            "label": "live paper",
            "active": state.active,
            "last_scan_at": state.last_scan_at,
            "lockout_reason": state.lockout_reason,
            "open_positions": [p.to_dict() for p in state.open_positions],
            "completed_trades": [trade_to_dict(t) for t in state.completed_trades],
            "skipped_signals": state.skipped_signals[-100:],
            "top_candidates": state.top_candidates[:40],
            "daily_net_pnl": round(state.daily_pnl, 2),
            "state_path": str(PaperStateStore(config).path),
        }
    )
    return summary


def date_from_iso(value: str):
    from datetime import date

    return date.fromisoformat(value)
