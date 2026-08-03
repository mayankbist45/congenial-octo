"""Shared domain types passed between pipeline stages.

These are plain dataclasses rather than ORM rows: the pipeline computes them
fresh each run, and only decisions and fills are persisted (see
``src/journal``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple


class Action(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    TRIM = "TRIM"
    ADD = "ADD"
    HOLD = "HOLD"


class TriggerType(str, Enum):
    """The six professional sell disciplines, plus portfolio-level reasons."""

    STOP_LOSS = "stop_loss"
    TRAILING_STOP = "trailing_stop"
    PROFIT_TARGET = "profit_target"
    TARGET_PRICE = "target_price"
    VALUATION_STRETCH = "valuation_stretch"
    FUNDAMENTAL_DETERIORATION = "fundamental_deterioration"
    OPPORTUNITY_COST = "opportunity_cost"
    DRIFT_BAND = "drift_band"
    SECTOR_CAP = "sector_cap"
    POSITION_CAP = "position_cap"
    RISK_LIMIT = "risk_limit"
    NEWS_EVENT = "news_event"
    QUARTERLY_REPORT_REVIEW = "quarterly_report_review"
    EARNINGS_EVENT = "earnings_event"
    UNDERWEIGHT_DEPLOY = "underweight_deploy"
    INITIAL_BUILD = "initial_build"


class Severity(str, Enum):
    """How much discretion the LLM has over a fired trigger."""

    MANDATORY = "mandatory"  # Rules-first: LLM cannot veto this
    REVIEW = "review"  # LLM decides whether to act
    OPPORTUNITY = "opportunity"  # Suggestion only


@dataclass
class UniverseEntry:
    symbol: str
    company_name: str
    sector: str
    isin: str = ""

    @property
    def yahoo_symbol(self) -> str:
        return f"{self.symbol}.NS"


@dataclass
class TechnicalSnapshot:
    """Price-derived indicators for a single symbol as of the last close."""

    symbol: str
    as_of: Optional[date] = None
    last_price: Optional[float] = None
    sma_50: Optional[float] = None
    sma_200: Optional[float] = None
    rsi_14: Optional[float] = None
    macd: Optional[float] = None
    macd_signal: Optional[float] = None
    macd_histogram: Optional[float] = None
    atr_14: Optional[float] = None
    atr_pct: Optional[float] = None
    return_1m: Optional[float] = None
    return_3m: Optional[float] = None
    return_6m: Optional[float] = None
    return_12m: Optional[float] = None
    volatility_annualised: Optional[float] = None
    avg_daily_turnover: Optional[float] = None
    distance_from_52w_high: Optional[float] = None
    distance_from_52w_low: Optional[float] = None
    above_sma_200: Optional[bool] = None
    golden_cross: Optional[bool] = None

    @property
    def has_price(self) -> bool:
        return self.last_price is not None and self.last_price > 0


@dataclass
class FundamentalSnapshot:
    """Company fundamentals, refreshed weekly rather than daily."""

    symbol: str
    as_of: Optional[datetime] = None
    source: str = ""
    market_cap: Optional[float] = None
    pe_ratio: Optional[float] = None
    forward_pe: Optional[float] = None
    peg_ratio: Optional[float] = None
    price_to_book: Optional[float] = None
    ev_to_ebitda: Optional[float] = None
    dividend_yield: Optional[float] = None
    roe_pct: Optional[float] = None
    roce_pct: Optional[float] = None
    operating_margin_pct: Optional[float] = None
    profit_margin_pct: Optional[float] = None
    debt_to_equity: Optional[float] = None
    current_ratio: Optional[float] = None
    interest_coverage: Optional[float] = None
    revenue_growth_pct: Optional[float] = None
    earnings_growth_pct: Optional[float] = None
    eps: Optional[float] = None
    free_cash_flow: Optional[float] = None
    cash_conversion: Optional[float] = None
    promoter_holding_pct: Optional[float] = None
    promoter_pledge_pct: Optional[float] = None
    analyst_target_price: Optional[float] = None
    analyst_recommendation: Optional[str] = None
    latest_quarter_label: Optional[str] = None

    @property
    def is_usable(self) -> bool:
        """True when there is enough data to score the name at all."""
        return self.pe_ratio is not None or self.roe_pct is not None


@dataclass
class FactorScores:
    """Normalized factor scores and the composite used for ranking."""

    symbol: str
    quality: float = 0.0
    value: float = 0.0
    growth: float = 0.0
    momentum: float = 0.0
    trend: float = 0.0
    composite: float = 0.0
    rank: Optional[int] = None
    percentile: Optional[float] = None
    passes_gates: bool = False
    gate_failures: List[str] = field(default_factory=list)
    contributing_metrics: Dict[str, float] = field(default_factory=dict)


@dataclass
class RiskMetrics:
    """Factor-model risk decomposition for one position."""

    symbol: str
    beta: Optional[float] = None
    idiosyncratic_vol: Optional[float] = None
    total_vol: Optional[float] = None
    systematic_vol: Optional[float] = None
    idio_share_pct: Optional[float] = None
    r_squared: Optional[float] = None
    risk_contribution_pct: Optional[float] = None
    marginal_risk: Optional[float] = None
    style_exposures: Dict[str, float] = field(default_factory=dict)
    days_to_liquidate: Optional[float] = None
    pct_of_adv: Optional[float] = None


@dataclass
class MarketRegimeSnapshot:
    """Price-derived read on the broad market backdrop."""

    as_of: Optional[date] = None
    regime: str = "unknown"
    score: Optional[float] = None
    components: Dict[str, float] = field(default_factory=dict)
    evidence: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


@dataclass
class PortfolioRiskProfile:
    """Portfolio-level risk aggregates.

    Two decompositions are reported because they answer different questions:

    - ``factor_risk_share_pct`` / ``idio_risk_share_pct`` split *total* risk,
      which is dominated by market beta for any long-only equity fund.
    - ``active_factor_share_pct`` / ``active_selection_share_pct`` split
      *active* (benchmark-relative) risk. This is the number that says whether
      the fund is being paid for stock picking or for unintended factor bets,
      and it is the one carrying a configured limit.
    """

    as_of: Optional[date] = None
    portfolio_beta: Optional[float] = None
    portfolio_vol_annualised: Optional[float] = None
    factor_risk_share_pct: Optional[float] = None
    idio_risk_share_pct: Optional[float] = None
    active_factor_share_pct: Optional[float] = None
    active_selection_share_pct: Optional[float] = None
    tracking_error_pct: Optional[float] = None
    active_share_pct: Optional[float] = None
    max_pairwise_correlation: Optional[float] = None
    avg_pairwise_correlation: Optional[float] = None
    concentration_top5_pct: Optional[float] = None
    concentration_top10_pct: Optional[float] = None
    effective_positions: Optional[float] = None
    market_regime: Optional[MarketRegimeSnapshot] = None
    sector_weights: Dict[str, float] = field(default_factory=dict)
    style_tilts: Dict[str, float] = field(default_factory=dict)
    per_position: Dict[str, RiskMetrics] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)


@dataclass
class Position:
    """A live holding, enriched with everything needed to judge it."""

    symbol: str
    quantity: int
    avg_cost: float
    last_price: float
    sector: str = "Unknown"
    company_name: str = ""
    entry_date: Optional[date] = None
    peak_price: Optional[float] = None
    entry_pe: Optional[float] = None
    thesis: str = ""
    target_price: Optional[float] = None

    @property
    def market_value(self) -> float:
        return self.quantity * self.last_price

    @property
    def cost_basis(self) -> float:
        return self.quantity * self.avg_cost

    @property
    def unrealised_pnl(self) -> float:
        return self.market_value - self.cost_basis

    @property
    def return_pct(self) -> float:
        if self.avg_cost <= 0:
            return 0.0
        return (self.last_price / self.avg_cost - 1.0) * 100.0

    @property
    def drawdown_from_peak_pct(self) -> float:
        if not self.peak_price or self.peak_price <= 0:
            return 0.0
        return (self.last_price / self.peak_price - 1.0) * 100.0

    def holding_days(self, as_of: Optional[date] = None) -> Optional[int]:
        """Days held, never negative.

        A position opened today is marked against the last available close, which
        may be a day or two old. That would otherwise report a negative tenure and
        wrongly age the position out of its grace period.
        """
        if self.entry_date is None:
            return None
        reference = as_of or date.today()
        return max((reference - self.entry_date).days, 0)


@dataclass
class PortfolioState:
    """Full snapshot of the fund at a point in time."""

    as_of: date
    cash: float
    positions: List[Position] = field(default_factory=list)
    realised_pnl_to_date: float = 0.0

    @property
    def invested_value(self) -> float:
        return sum(p.market_value for p in self.positions)

    @property
    def total_value(self) -> float:
        return self.invested_value + self.cash

    @property
    def cash_pct(self) -> float:
        total = self.total_value
        return (self.cash / total * 100.0) if total > 0 else 0.0

    def weight_of(self, symbol: str) -> float:
        total = self.total_value
        if total <= 0:
            return 0.0
        for position in self.positions:
            if position.symbol == symbol:
                return position.market_value / total * 100.0
        return 0.0

    def weights(self) -> Dict[str, float]:
        total = self.total_value
        if total <= 0:
            return {}
        return {p.symbol: p.market_value / total * 100.0 for p in self.positions}

    def sector_weights(self) -> Dict[str, float]:
        total = self.total_value
        if total <= 0:
            return {}
        out: Dict[str, float] = {}
        for position in self.positions:
            out[position.sector] = (
                out.get(position.sector, 0.0) + position.market_value / total * 100.0
            )
        return out

    def get(self, symbol: str) -> Optional[Position]:
        for position in self.positions:
            if position.symbol == symbol:
                return position
        return None

    @property
    def symbols(self) -> List[str]:
        return [p.symbol for p in self.positions]


@dataclass
class QuarterlyReport:
    """One quarterly results row-set normalized into key metrics."""

    symbol: str
    fiscal_period: str
    report_key: str
    source: str = "screener.in"
    fetched_at: Optional[datetime] = None
    period_end: Optional[date] = None
    revenue: Optional[float] = None
    operating_profit: Optional[float] = None
    opm_pct: Optional[float] = None
    net_profit: Optional[float] = None
    eps: Optional[float] = None
    interest: Optional[float] = None
    borrowings: Optional[float] = None
    promoter_holding_pct: Optional[float] = None
    promoter_pledge_pct: Optional[float] = None
    revenue_yoy_pct: Optional[float] = None
    profit_yoy_pct: Optional[float] = None
    eps_yoy_pct: Optional[float] = None
    opm_delta_pct: Optional[float] = None
    debt_delta_pct: Optional[float] = None


@dataclass
class QuarterlyReportAnalysis:
    """Persona-specific interpretation of a newly seen quarterly report."""

    symbol: str
    report_key: str
    run_date: date
    fiscal_period: str
    summary: str
    thesis_impact: str = "unclear"
    flags: List[str] = field(default_factory=list)
    highlights: Dict[str, str] = field(default_factory=dict)
    metrics: Dict[str, Optional[float]] = field(default_factory=dict)
    source: str = "heuristic"
    raw_error: str = ""


@dataclass
class QuarterlyReportContext:
    """Quarterly report state assembled for one run."""

    histories: Dict[str, List[QuarterlyReport]] = field(default_factory=dict)
    analyses: Dict[str, QuarterlyReportAnalysis] = field(default_factory=dict)
    new_symbols: List[str] = field(default_factory=list)
    errors: Dict[str, str] = field(default_factory=dict)


@dataclass
class NewsArticle:
    title: str
    url: str
    source: str
    published: Optional[datetime] = None
    summary: str = ""
    symbols: List[str] = field(default_factory=list)
    is_high_impact: bool = False
    matched_keywords: List[str] = field(default_factory=list)

    @property
    def fingerprint(self) -> str:
        """Normalized title used to deduplicate syndicated coverage."""
        cleaned = "".join(c.lower() for c in self.title if c.isalnum() or c == " ")
        return " ".join(cleaned.split())


@dataclass
class NewsDigest:
    as_of: date
    macro_summary: str = ""
    market_summary: str = ""
    holdings_notes: Dict[str, str] = field(default_factory=dict)
    watchlist_notes: Dict[str, str] = field(default_factory=dict)
    flagged_symbols: List[str] = field(default_factory=list)
    article_count: int = 0
    high_impact_count: int = 0
    articles: List[NewsArticle] = field(default_factory=list)
    generated_by_llm: bool = False


@dataclass
class EarningsAnalysis:
    """LLM or rules-only read of one company's latest quarterly results."""

    symbol: str
    period_end: date
    verdict: str = "in_line"
    quality_of_earnings: Optional[int] = None
    red_flags: List[str] = field(default_factory=list)
    thesis_impact: str = "intact"
    rationale: str = ""
    confidence: Optional[float] = None
    generated_by_llm: bool = False
    brief_excerpt: str = ""


@dataclass
class EarningsDigest:
    """Fresh quarterly analyses for holdings and watchlist names."""

    as_of: date
    analyses: Dict[str, EarningsAnalysis] = field(default_factory=dict)
    holdings_notes: Dict[str, str] = field(default_factory=dict)
    watchlist_notes: Dict[str, str] = field(default_factory=dict)
    flagged_symbols: List[str] = field(default_factory=list)
    symbols_polled: int = 0
    new_reports: int = 0


@dataclass
class Trigger:
    """A rule that fired and therefore justifies considering a trade."""

    symbol: str
    trigger_type: TriggerType
    severity: Severity
    detail: str
    suggested_action: Action = Action.HOLD
    suggested_quantity: Optional[int] = None
    metrics: Dict[str, Any] = field(default_factory=dict)


@dataclass
class TradeProposal:
    """A proposed trade awaiting human approval."""

    symbol: str
    action: Action
    quantity: int
    rationale: str
    trigger_types: List[str] = field(default_factory=list)
    confidence: Optional[float] = None
    limit_price: Optional[float] = None
    reference_price: Optional[float] = None
    estimated_value: Optional[float] = None
    company_name: str = ""
    sector: str = ""
    source: str = "llm"
    warnings: List[str] = field(default_factory=list)
    thesis: str = ""
    target_price: Optional[float] = None

    @property
    def is_buy_side(self) -> bool:
        return self.action in (Action.BUY, Action.ADD)


@dataclass
class DailyDecision:
    """The complete output of one daily cycle."""

    as_of: date
    is_deep_review: bool
    proposals: List[TradeProposal] = field(default_factory=list)
    # Proposals the risk engine refused, paired with the reason. Kept so the
    # journal records where the decision layer pushed against its limits.
    rejected: List[Tuple[TradeProposal, str]] = field(default_factory=list)
    memo: str = ""
    triggers: List[Trigger] = field(default_factory=list)
    turnover_used_pct: float = 0.0
    turnover_budget_pct: float = 0.0
    risk_profile: Optional[PortfolioRiskProfile] = None
    digest: Optional[NewsDigest] = None
    llm_used: bool = False

    @property
    def has_trades(self) -> bool:
        return len(self.proposals) > 0
