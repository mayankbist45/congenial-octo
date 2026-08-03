"""Domain objects for intraday strategy simulation."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class IntradaySignal:
    symbol: str
    strategy: str
    side: str
    entry_time: datetime
    entry_price: float
    stop_price: float
    target_price: float
    reason: str
    max_exit_time: Optional[datetime] = None

    @property
    def risk_per_share(self) -> float:
        return abs(self.entry_price - self.stop_price)


@dataclass
class RankedStock:
    symbol: str
    score: float
    turnover: float = 0.0
    volume_expansion: float = 0.0
    range_pct: float = 0.0
    gap_pct: Optional[float] = None
    vwap_distance_pct: Optional[float] = None
    prior_level_distance_pct: Optional[float] = None
    trend_cleanliness: float = 0.0
    reasons: List[str] = field(default_factory=list)


@dataclass
class IntradayTrade:
    symbol: str
    strategy: str
    side: str
    quantity: int
    entry_time: datetime
    exit_time: datetime
    entry_price: float
    exit_price: float
    stop_price: float
    target_price: float
    exit_reason: str
    gross_pnl: float
    costs: float
    net_pnl: float
    r_multiple: float

    @property
    def trade_date(self) -> date:
        return self.entry_time.date()


@dataclass
class PaperPosition:
    symbol: str
    strategy: str
    side: str
    quantity: int
    entry_time: datetime
    entry_price: float
    stop_price: float
    target_price: float
    max_exit_time: Optional[datetime] = None
    reason: str = ""

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "PaperPosition":
        data = dict(payload)
        data["entry_time"] = datetime.fromisoformat(str(data["entry_time"]))
        if data.get("max_exit_time"):
            data["max_exit_time"] = datetime.fromisoformat(str(data["max_exit_time"]))
        return cls(**data)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "strategy": self.strategy,
            "side": self.side,
            "quantity": self.quantity,
            "entry_time": self.entry_time.isoformat(),
            "entry_price": self.entry_price,
            "stop_price": self.stop_price,
            "target_price": self.target_price,
            "max_exit_time": self.max_exit_time.isoformat() if self.max_exit_time else None,
            "reason": self.reason,
        }


@dataclass
class PaperState:
    persona_id: str
    trading_date: str
    active: bool = False
    last_scan_at: Optional[str] = None
    lockout_reason: Optional[str] = None
    open_positions: List[PaperPosition] = field(default_factory=list)
    completed_trades: List[IntradayTrade] = field(default_factory=list)
    skipped_signals: List[Dict[str, Any]] = field(default_factory=list)
    top_candidates: List[Dict[str, Any]] = field(default_factory=list)
    daily_pnl: float = 0.0

    @property
    def trade_count(self) -> int:
        return len(self.completed_trades) + len(self.open_positions)


@dataclass
class IntradayBacktestResult:
    persona_id: str
    start_date: date
    end_date: date
    starting_capital: float
    ending_capital: float
    trades: List[IntradayTrade] = field(default_factory=list)
    equity_curve: List[tuple[date, float]] = field(default_factory=list)
    strategy_pnl: Dict[str, float] = field(default_factory=dict)
    daily_pnl: Dict[str, float] = field(default_factory=dict)

    @property
    def total_net_pnl(self) -> float:
        return self.ending_capital - self.starting_capital

    @property
    def total_return_pct(self) -> float:
        if self.starting_capital <= 0:
            return 0.0
        return self.total_net_pnl / self.starting_capital * 100.0
