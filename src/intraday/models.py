"""Data models for intraday paper trading."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Dict, List, Optional


@dataclass
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
    pair_id: Optional[str] = None
    pair_leg: Optional[str] = None
    pair_side: Optional[str] = None
    pair_entry_price: Optional[float] = None
    pair_stop_price: Optional[float] = None
    pair_target_price: Optional[float] = None
    entry_z: Optional[float] = None
    pair_beta: Optional[float] = None
    use_trailing_stop: bool = True

    @property
    def is_pair(self) -> bool:
        return bool(self.pair_leg and self.pair_side)

    @property
    def risk_per_share(self) -> float:
        return abs(self.entry_price - self.stop_price)


@dataclass
class IntradayTrade:
    symbol: str
    strategy: str
    side: str
    quantity: float
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
    pair_id: Optional[str] = None

    @property
    def trade_date(self) -> date:
        return self.entry_time.date()


@dataclass
class RankedStock:
    symbol: str
    score: float
    turnover: float
    volume_expansion: float
    range_pct: float
    gap_pct: Optional[float]
    vwap_distance_pct: Optional[float]
    prior_level_distance_pct: Optional[float]
    trend_cleanliness: float
    in_play: bool = False
    ofi_z: Optional[float] = None
    spread_z: Optional[float] = None
    pair_id: Optional[str] = None
    kronos_enabled: bool = False
    kronos_mode: Optional[str] = None
    kronos_score: Optional[float] = None
    kronos_expected_return_pct: Optional[float] = None
    kronos_confidence: Optional[float] = None
    kronos_trend_bias: Optional[str] = None
    kronos_rationale: Optional[str] = None
    reasons: List[str] = field(default_factory=list)


@dataclass
class PaperPosition:
    symbol: str
    strategy: str
    side: str
    quantity: float
    entry_time: datetime
    entry_price: float
    stop_price: float
    target_price: float
    max_exit_time: Optional[datetime]
    reason: str
    trailing_stop_pct: Optional[float] = None
    peak_price: Optional[float] = None
    entry_order_id: Optional[str] = None
    exit_order_id: Optional[str] = None
    pair_id: Optional[str] = None
    pair_partner: Optional[str] = None
    entry_z: Optional[float] = None
    pair_beta: Optional[float] = None
    use_trailing_stop: bool = True


@dataclass
class PaperState:
    persona_id: str
    trading_date: date
    active: bool = True
    last_scan_at: Optional[str] = None
    lockout_reason: Optional[str] = None
    daily_pnl: float = 0.0
    open_positions: List[PaperPosition] = field(default_factory=list)
    completed_trades: List[IntradayTrade] = field(default_factory=list)
    skipped_signals: List[Dict[str, object]] = field(default_factory=list)
    top_candidates: List[Dict[str, object]] = field(default_factory=list)
    watchlist: List[Dict[str, object]] = field(default_factory=list)
    style_profiles: Dict[str, Dict[str, object]] = field(default_factory=dict)
    style_params: Dict[str, object] = field(default_factory=dict)

    @property
    def trade_count(self) -> int:
        return len(self.completed_trades)


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
        return sum(t.net_pnl for t in self.trades)

    @property
    def total_return_pct(self) -> float:
        if self.starting_capital <= 0:
            return 0.0
        return (self.ending_capital / self.starting_capital - 1.0) * 100.0
