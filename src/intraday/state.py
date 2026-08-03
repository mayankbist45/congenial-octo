"""JSON persistence for live intraday paper trading state."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.config import Config
from src.intraday.models import IntradayTrade, PaperPosition, PaperState


class PaperStateStore:
    def __init__(self, config: Config, path: Optional[Path] = None):
        self._config = config
        self.path = path or config.root / "data" / "intraday" / f"{config.persona_id}_state.json"

    def load(self, trading_date: Optional[date] = None) -> PaperState:
        today = (trading_date or date.today()).isoformat()
        if not self.path.exists():
            return PaperState(persona_id=self._config.persona_id, trading_date=today)
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            state = self._from_payload(payload)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return PaperState(persona_id=self._config.persona_id, trading_date=today)
        if state.trading_date != today:
            return PaperState(persona_id=self._config.persona_id, trading_date=today)
        return state

    def save(self, state: PaperState) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.to_payload(state), indent=2, default=str), encoding="utf-8")
        return self.path

    @staticmethod
    def to_payload(state: PaperState) -> Dict[str, Any]:
        return {
            "persona_id": state.persona_id,
            "trading_date": state.trading_date,
            "active": state.active,
            "last_scan_at": state.last_scan_at,
            "lockout_reason": state.lockout_reason,
            "open_positions": [p.to_dict() for p in state.open_positions],
            "completed_trades": [trade_to_dict(t) for t in state.completed_trades],
            "skipped_signals": state.skipped_signals[-100:],
            "top_candidates": state.top_candidates[:40],
            "daily_pnl": round(state.daily_pnl, 2),
        }

    @staticmethod
    def _from_payload(payload: Dict[str, Any]) -> PaperState:
        return PaperState(
            persona_id=str(payload.get("persona_id") or "calan"),
            trading_date=str(payload.get("trading_date") or date.today().isoformat()),
            active=bool(payload.get("active")),
            last_scan_at=payload.get("last_scan_at"),
            lockout_reason=payload.get("lockout_reason"),
            open_positions=[
                PaperPosition.from_dict(row)
                for row in payload.get("open_positions", [])
                if isinstance(row, dict)
            ],
            completed_trades=[
                trade_from_dict(row)
                for row in payload.get("completed_trades", [])
                if isinstance(row, dict)
            ],
            skipped_signals=[
                row for row in payload.get("skipped_signals", [])
                if isinstance(row, dict)
            ],
            top_candidates=[
                row for row in payload.get("top_candidates", [])
                if isinstance(row, dict)
            ],
            daily_pnl=float(payload.get("daily_pnl") or 0.0),
        )


def trade_to_dict(trade: IntradayTrade) -> Dict[str, Any]:
    payload = asdict(trade)
    payload["entry_time"] = trade.entry_time.isoformat()
    payload["exit_time"] = trade.exit_time.isoformat()
    return payload


def trade_from_dict(payload: Dict[str, Any]) -> IntradayTrade:
    data = dict(payload)
    data["entry_time"] = datetime.fromisoformat(str(data["entry_time"]))
    data["exit_time"] = datetime.fromisoformat(str(data["exit_time"]))
    return IntradayTrade(**data)
