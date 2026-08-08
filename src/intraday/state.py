"""Persist intraday paper state between polls."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from src.config import Config
from src.intraday.models import IntradayTrade, PaperPosition, PaperState


class PaperStateStore:
    def __init__(self, config: Config):
        self._config = config
        self._path = config.intraday_dir / f"{config.persona_id}_state.json"

    def load(self, trading_date: date) -> PaperState:
        if not self._path.exists():
            return PaperState(
                persona_id=self._config.persona_id,
                trading_date=trading_date,
            )
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return PaperState(
                persona_id=self._config.persona_id,
                trading_date=trading_date,
            )
        if payload.get("trading_date") != trading_date.isoformat():
            return PaperState(
                persona_id=self._config.persona_id,
                trading_date=trading_date,
            )
        return _state_from_dict(payload)

    def save(self, state: PaperState) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(_state_to_dict(state), indent=2), encoding="utf-8")


def _state_to_dict(state: PaperState) -> dict:
    return {
        "persona_id": state.persona_id,
        "trading_date": state.trading_date.isoformat(),
        "active": state.active,
        "last_scan_at": state.last_scan_at,
        "lockout_reason": state.lockout_reason,
        "daily_pnl": state.daily_pnl,
        "watchlist": state.watchlist,
        "open_positions": [_position_to_dict(p) for p in state.open_positions],
        "completed_trades": [_trade_to_dict(t) for t in state.completed_trades],
        "skipped_signals": state.skipped_signals,
        "top_candidates": state.top_candidates,
        "style_profiles": state.style_profiles,
        "style_params": state.style_params,
    }


def _state_from_dict(payload: dict) -> PaperState:
    return PaperState(
        persona_id=str(payload.get("persona_id", "")),
        trading_date=date.fromisoformat(str(payload["trading_date"])),
        active=bool(payload.get("active", True)),
        last_scan_at=payload.get("last_scan_at"),
        lockout_reason=payload.get("lockout_reason"),
        daily_pnl=float(payload.get("daily_pnl", 0.0) or 0.0),
        watchlist=list(payload.get("watchlist") or []),
        open_positions=[_position_from_dict(p) for p in payload.get("open_positions") or []],
        completed_trades=[_trade_from_dict(t) for t in payload.get("completed_trades") or []],
        skipped_signals=list(payload.get("skipped_signals") or []),
        top_candidates=list(payload.get("top_candidates") or []),
        style_profiles=dict(payload.get("style_profiles") or {}),
        style_params=dict(payload.get("style_params") or {}),
    )


def _position_to_dict(p: PaperPosition) -> dict:
    return {
        "symbol": p.symbol,
        "strategy": p.strategy,
        "side": p.side,
        "quantity": p.quantity,
        "entry_time": p.entry_time.isoformat(sep=" "),
        "entry_price": p.entry_price,
        "stop_price": p.stop_price,
        "target_price": p.target_price,
        "max_exit_time": p.max_exit_time.isoformat(sep=" ") if p.max_exit_time else None,
        "reason": p.reason,
        "trailing_stop_pct": p.trailing_stop_pct,
        "peak_price": p.peak_price,
        "entry_order_id": p.entry_order_id,
        "exit_order_id": p.exit_order_id,
        "pair_id": p.pair_id,
        "pair_partner": p.pair_partner,
        "entry_z": p.entry_z,
        "pair_beta": p.pair_beta,
        "use_trailing_stop": p.use_trailing_stop,
    }


def _position_from_dict(payload: dict) -> PaperPosition:
    from datetime import datetime

    max_exit = payload.get("max_exit_time")
    return PaperPosition(
        symbol=str(payload["symbol"]),
        strategy=str(payload["strategy"]),
        side=str(payload["side"]),
        quantity=float(payload["quantity"]),
        entry_time=datetime.fromisoformat(str(payload["entry_time"])),
        entry_price=float(payload["entry_price"]),
        stop_price=float(payload["stop_price"]),
        target_price=float(payload["target_price"]),
        max_exit_time=datetime.fromisoformat(str(max_exit)) if max_exit else None,
        reason=str(payload.get("reason", "")),
        trailing_stop_pct=payload.get("trailing_stop_pct"),
        peak_price=payload.get("peak_price"),
        entry_order_id=payload.get("entry_order_id"),
        exit_order_id=payload.get("exit_order_id"),
        pair_id=payload.get("pair_id"),
        pair_partner=payload.get("pair_partner"),
        entry_z=payload.get("entry_z"),
        pair_beta=payload.get("pair_beta"),
        use_trailing_stop=bool(payload.get("use_trailing_stop", True)),
    )


def _trade_to_dict(t: IntradayTrade) -> dict:
    return {
        "symbol": t.symbol,
        "strategy": t.strategy,
        "side": t.side,
        "quantity": t.quantity,
        "entry_time": t.entry_time.isoformat(sep=" "),
        "exit_time": t.exit_time.isoformat(sep=" "),
        "entry_price": t.entry_price,
        "exit_price": t.exit_price,
        "stop_price": t.stop_price,
        "target_price": t.target_price,
        "exit_reason": t.exit_reason,
        "gross_pnl": t.gross_pnl,
        "costs": t.costs,
        "net_pnl": t.net_pnl,
        "r_multiple": t.r_multiple,
    }


def _trade_from_dict(payload: dict) -> IntradayTrade:
    from datetime import datetime

    return IntradayTrade(
        symbol=str(payload["symbol"]),
        strategy=str(payload["strategy"]),
        side=str(payload["side"]),
        quantity=float(payload["quantity"]),
        entry_time=datetime.fromisoformat(str(payload["entry_time"])),
        exit_time=datetime.fromisoformat(str(payload["exit_time"])),
        entry_price=float(payload["entry_price"]),
        exit_price=float(payload["exit_price"]),
        stop_price=float(payload["stop_price"]),
        target_price=float(payload["target_price"]),
        exit_reason=str(payload["exit_reason"]),
        gross_pnl=float(payload["gross_pnl"]),
        costs=float(payload["costs"]),
        net_pnl=float(payload["net_pnl"]),
        r_multiple=float(payload["r_multiple"]),
    )
