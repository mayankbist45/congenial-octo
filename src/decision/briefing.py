"""Daily briefing construction.

Assembles everything the decision layer sees into one text document: the
portfolio with per-position P&L and factor scores, the risk profile, the
triggers that fired, the screened candidates, the news digest, and the remaining
turnover budget.

The briefing is written the way a research note would be, because that is what
the model reasons over best, and because a human reviewing the fund later should
be able to read exactly what the manager knew at the time. Each briefing is
saved to disk for that reason.
"""

from __future__ import annotations

from datetime import date
from typing import Dict, List, Optional

from src.config import Config, get_config
from src.logging_setup import get_logger
from src.models import (
    FactorScores,
    FundamentalSnapshot,
    NewsDigest,
    PortfolioRiskProfile,
    PortfolioState,
    Severity,
    TechnicalSnapshot,
    Trigger,
)
from src.risk.turnover import TurnoverStatus

logger = get_logger(__name__)


def _money(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    if abs(value) >= 1e7:
        return f"Rs {value / 1e7:.2f}cr"
    if abs(value) >= 1e5:
        return f"Rs {value / 1e5:.2f}L"
    return f"Rs {value:,.0f}"


def _number(value: Optional[float], suffix: str = "", places: int = 1) -> str:
    if value is None:
        return "n/a"
    return f"{value:.{places}f}{suffix}"


class BriefingBuilder:
    """Builds the daily briefing document."""

    def __init__(self, config: Optional[Config] = None):
        self._config = config or get_config()

    def build(
        self,
        portfolio: PortfolioState,
        scores: Dict[str, FactorScores],
        fundamentals: Dict[str, FundamentalSnapshot],
        technicals: Dict[str, TechnicalSnapshot],
        triggers: List[Trigger],
        candidates: List[FactorScores],
        digest: Optional[NewsDigest],
        risk_profile: Optional[PortfolioRiskProfile],
        turnover: TurnoverStatus,
        is_deep_review: bool,
        as_of: Optional[date] = None,
    ) -> str:
        as_of = as_of or portfolio.as_of
        sections: List[str] = []

        sections.append(self._header(as_of, is_deep_review, portfolio, turnover))
        sections.append(self._mandate())
        sections.append(self._holdings(portfolio, scores, fundamentals, technicals, risk_profile))
        sections.append(self._risk(risk_profile))
        sections.append(self._triggers(triggers))
        sections.append(self._candidates(candidates, fundamentals, technicals))
        sections.append(self._news(digest, portfolio))

        briefing = "\n\n".join(section for section in sections if section)
        self._save(briefing, as_of)
        return briefing

    # ------------------------------------------------------------------
    def _header(
        self,
        as_of: date,
        is_deep_review: bool,
        portfolio: PortfolioState,
        turnover: TurnoverStatus,
    ) -> str:
        mode = (
            "QUARTERLY DEEP REVIEW - full portfolio reconsideration"
            if is_deep_review
            else "DAILY MONITORING - act only if a trigger fired"
        )
        return "\n".join(
            [
                "=" * 78,
                f"DAILY BRIEFING - {as_of.strftime('%A, %d %B %Y')}",
                f"MODE: {mode}",
                "=" * 78,
                "",
                f"Fund value      : {_money(portfolio.total_value)}",
                f"Invested        : {_money(portfolio.invested_value)} "
                f"({100 - portfolio.cash_pct:.1f}%)",
                f"Cash            : {_money(portfolio.cash)} "
                f"({portfolio.cash_pct:.1f}%)",
                f"Positions       : {len(portfolio.positions)} of "
                f"{self._config.get('fund.target_positions')} target",
                f"Realised P&L    : {_money(portfolio.realised_pnl_to_date)}",
                f"Turnover budget : {turnover.describe()}",
            ]
        )

    def _mandate(self) -> str:
        risk = self._config.risk
        return "\n".join(
            [
                "--- MANDATE AND HARD LIMITS ---",
                f"Universe            : Nifty 500 (NSE), delivery trades only",
                f"Benchmark           : {self._config.get('fund.benchmark_label')}",
                f"Max per position    : {risk.get('max_position_pct')}%",
                f"Max per sector      : {risk.get('max_sector_pct')}%",
                f"Min cash buffer     : {self._config.get('fund.min_cash_buffer_pct')}%",
                f"Hard stop loss      : {risk.get('stop_loss_pct')}% from cost",
                f"Profit review level : +{risk.get('profit_review_pct')}% from cost",
                f"Annual turnover cap : {risk.get('annual_turnover_budget_pct')}%",
                "",
                "These limits are enforced mechanically after you respond. A "
                "proposal that breaches one is discarded, so proposing it only "
                "wastes the opportunity.",
            ]
        )

    def _holdings(
        self,
        portfolio: PortfolioState,
        scores: Dict[str, FactorScores],
        fundamentals: Dict[str, FundamentalSnapshot],
        technicals: Dict[str, TechnicalSnapshot],
        risk_profile: Optional[PortfolioRiskProfile],
    ) -> str:
        if not portfolio.positions:
            return (
                "--- CURRENT HOLDINGS ---\n"
                "The portfolio is empty. This is the initial build: select names "
                "from the candidate list below to construct a diversified "
                "starting portfolio."
            )

        lines = ["--- CURRENT HOLDINGS ---"]
        weights = portfolio.weights()

        for position in sorted(
            portfolio.positions, key=lambda p: weights.get(p.symbol, 0), reverse=True
        ):
            symbol = position.symbol
            score = scores.get(symbol)
            fundamental = fundamentals.get(symbol)
            technical = technicals.get(symbol)
            metrics = risk_profile.per_position.get(symbol) if risk_profile else None

            holding_days = position.holding_days(portfolio.as_of)
            lines.append("")
            lines.append(
                f"{symbol} ({position.company_name}) - {position.sector}"
            )
            lines.append(
                f"  weight {weights.get(symbol, 0):.2f}% | "
                f"{position.quantity} shares @ avg {position.avg_cost:.2f} | "
                f"now {position.last_price:.2f} | "
                f"P&L {position.return_pct:+.1f}% ({_money(position.unrealised_pnl)})"
            )
            if holding_days is not None:
                lines.append(
                    f"  held {holding_days} days | peak "
                    f"{_number(position.peak_price, places=2)} "
                    f"({position.drawdown_from_peak_pct:+.1f}% from peak)"
                )
            if position.thesis:
                lines.append(f"  original thesis: {position.thesis}")
            if position.target_price:
                lines.append(f"  target price at entry: {position.target_price:.2f}")

            if score:
                lines.append(
                    f"  factor scores: composite {score.composite:+.2f} "
                    f"(quality {score.quality:+.2f}, value {score.value:+.2f}, "
                    f"growth {score.growth:+.2f}, momentum {score.momentum:+.2f}, "
                    f"trend {score.trend:+.2f}) | universe percentile "
                    f"{_number(score.percentile, '', 0)}"
                )
                if not score.passes_gates:
                    lines.append(
                        f"  WARNING fails entry gates: {'; '.join(score.gate_failures)}"
                    )
            if fundamental:
                lines.append(
                    f"  fundamentals: P/E {_number(fundamental.pe_ratio)} | "
                    f"ROE {_number(fundamental.roe_pct, '%')} | "
                    f"ROCE {_number(fundamental.roce_pct, '%')} | "
                    f"D/E {_number(fundamental.debt_to_equity, '', 2)} | "
                    f"rev growth {_number(fundamental.revenue_growth_pct, '%')} | "
                    f"EPS growth {_number(fundamental.earnings_growth_pct, '%')}"
                )
                if position.entry_pe and fundamental.pe_ratio:
                    lines.append(
                        f"  P/E at entry {position.entry_pe:.1f} -> now "
                        f"{fundamental.pe_ratio:.1f}"
                    )
            if technical:
                trend = (
                    "above 200-DMA" if technical.above_sma_200 else "below 200-DMA"
                )
                lines.append(
                    f"  technicals: RSI {_number(technical.rsi_14, '', 0)} | "
                    f"{trend} | 6m {_number(technical.return_6m, '%')} | "
                    f"12m {_number(technical.return_12m, '%')} | "
                    f"vol {_number(technical.volatility_annualised, '%')}"
                )
            if metrics:
                lines.append(
                    f"  risk: beta {_number(metrics.beta, '', 2)} | "
                    f"idio vol {_number(metrics.idiosyncratic_vol, '%')} | "
                    f"contributes {_number(metrics.risk_contribution_pct, '%')} "
                    f"of portfolio risk"
                )

        lines.append("")
        lines.append("Sector exposure:")
        for sector, weight in sorted(
            portfolio.sector_weights().items(), key=lambda kv: kv[1], reverse=True
        ):
            lines.append(f"  {sector}: {weight:.1f}%")

        return "\n".join(lines)

    def _risk(self, profile: Optional[PortfolioRiskProfile]) -> str:
        if profile is None:
            return ""

        lines = ["--- PORTFOLIO RISK PROFILE ---"]
        lines.append(
            f"Beta vs benchmark        : {_number(profile.portfolio_beta, '', 2)}"
        )
        lines.append(
            f"Total volatility         : "
            f"{_number(profile.portfolio_vol_annualised, '%')} annualised"
        )
        lines.append(
            f"Tracking error           : {_number(profile.tracking_error_pct, '%')}"
        )
        lines.append(
            f"  from stock selection   : "
            f"{_number(profile.active_selection_share_pct, '%')}"
        )
        lines.append(
            f"  from factor and sector : "
            f"{_number(profile.active_factor_share_pct, '%')} "
            f"(risk the fund is not paid to take)"
        )
        lines.append(
            f"Active share             : {_number(profile.active_share_pct, '%')}"
        )
        lines.append(
            f"Top 5 concentration      : "
            f"{_number(profile.concentration_top5_pct, '%')}"
        )
        lines.append(
            f"Effective positions      : "
            f"{_number(profile.effective_positions, '', 1)} "
            f"(diversification after accounting for position sizes)"
        )
        lines.append(
            f"Avg pairwise correlation : "
            f"{_number(profile.avg_pairwise_correlation, '', 2)} | max "
            f"{_number(profile.max_pairwise_correlation, '', 2)}"
        )
        if profile.style_tilts:
            tilts = ", ".join(
                f"{name} {value:+.2f}" for name, value in profile.style_tilts.items()
            )
            lines.append(f"Style tilts              : {tilts}")

        if profile.warnings:
            lines.append("")
            lines.append("RISK LIMIT BREACHES:")
            for warning in profile.warnings:
                lines.append(f"  - {warning}")

        return "\n".join(lines)

    def _triggers(self, triggers: List[Trigger]) -> str:
        if not triggers:
            return (
                "--- TRIGGERS FIRED TODAY ---\n"
                "None. No sell rule broke, no limit was breached and no drift "
                "band was exceeded.\n\n"
                "This is the expected outcome on most days. Unless the news "
                "below changes an investment case, the correct answer today is "
                "no trades."
            )

        by_severity: Dict[Severity, List[Trigger]] = {}
        for trigger in triggers:
            by_severity.setdefault(trigger.severity, []).append(trigger)

        lines = ["--- TRIGGERS FIRED TODAY ---"]

        mandatory = by_severity.get(Severity.MANDATORY, [])
        if mandatory:
            lines.append("")
            lines.append(
                "MANDATORY (already executed by the risk engine - do not "
                "re-propose these, and account for them in your reasoning):"
            )
            for trigger in mandatory:
                lines.append(
                    f"  [{trigger.trigger_type.value}] {trigger.symbol}: "
                    f"{trigger.detail}"
                )

        review = by_severity.get(Severity.REVIEW, [])
        if review:
            lines.append("")
            lines.append("FOR YOUR DECISION (you judge whether to act):")
            for trigger in review:
                lines.append(
                    f"  [{trigger.trigger_type.value}] {trigger.symbol}: "
                    f"{trigger.detail}"
                )

        opportunity = by_severity.get(Severity.OPPORTUNITY, [])
        if opportunity:
            lines.append("")
            lines.append("OPPORTUNITIES (act only if clearly worth the turnover):")
            for trigger in opportunity:
                lines.append(
                    f"  [{trigger.trigger_type.value}] {trigger.symbol}: "
                    f"{trigger.detail}"
                )

        return "\n".join(lines)

    def _candidates(
        self,
        candidates: List[FactorScores],
        fundamentals: Dict[str, FundamentalSnapshot],
        technicals: Dict[str, TechnicalSnapshot],
    ) -> str:
        if not candidates:
            return (
                "--- SCREENED CANDIDATES ---\n"
                "No names currently clear both the fundamental gates and the "
                "minimum score. There is nothing worth buying today."
            )

        lines = [
            "--- SCREENED CANDIDATES ---",
            "Ranked by composite factor score. Every name here already passes "
            "the fund's fundamental gates and liquidity floor.",
        ]
        for index, score in enumerate(candidates, start=1):
            symbol = score.symbol
            fundamental = fundamentals.get(symbol)
            technical = technicals.get(symbol)
            lines.append("")
            lines.append(
                f"{index}. {symbol} - composite {score.composite:+.2f} "
                f"(percentile {_number(score.percentile, '', 0)})"
            )
            lines.append(
                f"   quality {score.quality:+.2f} | value {score.value:+.2f} | "
                f"growth {score.growth:+.2f} | momentum {score.momentum:+.2f} | "
                f"trend {score.trend:+.2f}"
            )
            if fundamental:
                lines.append(
                    f"   P/E {_number(fundamental.pe_ratio)} | "
                    f"ROE {_number(fundamental.roe_pct, '%')} | "
                    f"ROCE {_number(fundamental.roce_pct, '%')} | "
                    f"D/E {_number(fundamental.debt_to_equity, '', 2)} | "
                    f"rev growth {_number(fundamental.revenue_growth_pct, '%')} | "
                    f"mcap {_money(fundamental.market_cap)}"
                )
            if technical:
                lines.append(
                    f"   price {_number(technical.last_price, '', 2)} | "
                    f"RSI {_number(technical.rsi_14, '', 0)} | "
                    f"12m {_number(technical.return_12m, '%')} | "
                    f"vol {_number(technical.volatility_annualised, '%')} | "
                    f"ADV {_money(technical.avg_daily_turnover)}"
                )
        return "\n".join(lines)

    def _news(self, digest: Optional[NewsDigest], portfolio: PortfolioState) -> str:
        if digest is None:
            return "--- NEWS ---\nNo news digest available for today."

        lines = ["--- NEWS DIGEST ---"]
        lines.append(
            f"({digest.article_count} articles reviewed, "
            f"{digest.high_impact_count} high impact"
            + ("" if digest.generated_by_llm else ", keyword digest only")
            + ")"
        )
        if digest.macro_summary:
            lines.append("")
            lines.append(f"Macro: {digest.macro_summary}")
        if digest.market_summary:
            lines.append("")
            lines.append(f"Market: {digest.market_summary}")

        if digest.holdings_notes:
            lines.append("")
            lines.append("On current holdings:")
            for symbol, note in digest.holdings_notes.items():
                lines.append(f"  {symbol}: {note}")

        if digest.watchlist_notes:
            lines.append("")
            lines.append("On watchlist names:")
            for symbol, note in digest.watchlist_notes.items():
                lines.append(f"  {symbol}: {note}")

        if digest.flagged_symbols:
            lines.append("")
            lines.append(
                "FLAGGED for possible thesis change: "
                + ", ".join(digest.flagged_symbols)
            )

        return "\n".join(lines)

    def _save(self, briefing: str, as_of: date) -> None:
        path = self._config.briefing_dir / f"briefing_{as_of.isoformat()}.txt"
        try:
            path.write_text(briefing, encoding="utf-8")
            logger.info("Briefing saved to %s", path.name)
        except OSError as exc:
            logger.warning("Could not save briefing: %s", exc)
