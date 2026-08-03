"""LLM-driven earnings triggers (REVIEW/OPPORTUNITY only — never MANDATORY)."""

from __future__ import annotations

from typing import List, Optional, Set

from src.config import Config, get_config
from src.models import (
    Action,
    EarningsAnalysis,
    EarningsDigest,
    PortfolioState,
    Severity,
    Trigger,
    TriggerType,
)


class EarningsTriggerEngine:
    """Turn fresh earnings analyses into discretionary triggers."""

    def __init__(self, config: Optional[Config] = None):
        self._config = config or get_config()

    def evaluate(
        self,
        digest: EarningsDigest,
        portfolio: PortfolioState,
        candidate_symbols: Set[str],
        is_deep_review: bool,
    ) -> List[Trigger]:
        if not digest.analyses:
            return []

        triggers: List[Trigger] = []
        holdings = set(portfolio.symbols)

        for symbol, analysis in digest.analyses.items():
            if symbol in holdings:
                triggers.extend(self._holding_triggers(symbol, analysis))
            elif is_deep_review and symbol in candidate_symbols:
                triggers.extend(self._candidate_triggers(symbol, analysis))

        return triggers

    def _holding_triggers(self, symbol: str, analysis: EarningsAnalysis) -> List[Trigger]:
        if analysis.thesis_impact == "broken":
            return [
                Trigger(
                    symbol=symbol,
                    trigger_type=TriggerType.EARNINGS_EVENT,
                    severity=Severity.REVIEW,
                    detail=(
                        f"Quarterly results ({analysis.period_end.isoformat()}): "
                        f"thesis impact BROKEN — {analysis.verdict} vs trend. "
                        f"{analysis.rationale[:350]}"
                    ),
                    suggested_action=Action.TRIM,
                    metrics={
                        "verdict": analysis.verdict,
                        "thesis_impact": analysis.thesis_impact,
                        "quality": analysis.quality_of_earnings,
                        "red_flags": analysis.red_flags,
                    },
                )
            ]
        if analysis.thesis_impact == "weakens" or analysis.verdict == "miss":
            return [
                Trigger(
                    symbol=symbol,
                    trigger_type=TriggerType.EARNINGS_EVENT,
                    severity=Severity.REVIEW,
                    detail=(
                        f"Quarterly results ({analysis.period_end.isoformat()}): "
                        f"thesis weakens — {analysis.verdict}. "
                        f"{analysis.rationale[:350]}"
                    ),
                    suggested_action=Action.HOLD,
                    metrics={
                        "verdict": analysis.verdict,
                        "thesis_impact": analysis.thesis_impact,
                        "red_flags": analysis.red_flags,
                    },
                )
            ]
        return []

    def _candidate_triggers(
        self, symbol: str, analysis: EarningsAnalysis
    ) -> List[Trigger]:
        if analysis.verdict != "beat" or analysis.thesis_impact != "strengthens":
            return []
        return [
            Trigger(
                symbol=symbol,
                trigger_type=TriggerType.EARNINGS_EVENT,
                severity=Severity.OPPORTUNITY,
                detail=(
                    f"Fresh quarterly beat ({analysis.period_end.isoformat()}): "
                    f"{analysis.rationale[:350]}"
                ),
                suggested_action=Action.HOLD,
                metrics={
                    "verdict": analysis.verdict,
                    "thesis_impact": analysis.thesis_impact,
                    "quality": analysis.quality_of_earnings,
                },
            )
        ]
