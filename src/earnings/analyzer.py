"""Earnings analyst: LLM with a rules-only fallback."""

from __future__ import annotations

from datetime import date
from typing import Optional

import pandas as pd

from src.config import Config, get_config
from src.earnings.brief import build_brief
from src.earnings.prompts import SYSTEM_PROMPT, validate_payload
from src.llm import LLMClient, LLMUnavailable, extract_json
from src.logging_setup import get_logger
from src.models import EarningsAnalysis

logger = get_logger(__name__)

RETRY_REMINDER = "Return valid JSON only, matching the requested schema exactly."


class EarningsAnalyzer:
    def __init__(self, config: Optional[Config] = None, llm: Optional[LLMClient] = None):
        self._config = config or get_config()
        self._llm = llm or LLMClient(self._config)

    def analyze(
        self,
        symbol: str,
        company_name: str,
        sector: str,
        quarters: pd.DataFrame,
        period_end: date,
    ) -> EarningsAnalysis:
        brief = build_brief(symbol, company_name, sector, quarters, period_end)
        if self._config.get("earnings.llm_analyze", True) and self._llm.available:
            analysis = self._analyze_llm(symbol, period_end, brief)
            if analysis is not None:
                return analysis
        return self._analyze_rules(symbol, period_end, quarters, brief)

    def _analyze_llm(
        self, symbol: str, period_end: date, brief: str
    ) -> Optional[EarningsAnalysis]:
        model = str(self._config.get("llm.digest_model", self._llm.digest_model))
        for attempt in range(2):
            user = brief if attempt == 0 else f"{brief}\n\n{RETRY_REMINDER}"
            try:
                raw = self._llm.complete(SYSTEM_PROMPT, user, model=model, temperature=0.0)
            except LLMUnavailable as exc:
                logger.warning("Earnings LLM failed for %s: %s", symbol, exc)
                return None
            payload = validate_payload(extract_json(raw))
            if payload is None:
                continue
            return EarningsAnalysis(
                symbol=symbol,
                period_end=period_end,
                verdict=payload["verdict"],
                quality_of_earnings=payload["quality_of_earnings"],
                red_flags=payload["red_flags"],
                thesis_impact=payload["thesis_impact"],
                rationale=payload["rationale"],
                confidence=payload["confidence"],
                generated_by_llm=True,
                brief_excerpt=brief[:500],
            )
        logger.warning("Earnings LLM returned unusable JSON for %s", symbol)
        return None

    def _analyze_rules(
        self,
        symbol: str,
        period_end: date,
        quarters: pd.DataFrame,
        brief: str,
    ) -> EarningsAnalysis:
        frame = quarters.copy()
        frame["period_end"] = pd.to_datetime(frame["period_end"]).dt.date
        latest = frame[frame["period_end"] == period_end]
        if latest.empty:
            latest = frame.sort_values("period_end").tail(1)
        row = latest.iloc[-1]
        prior_period = period_end.replace(year=period_end.year - 1)
        prior = frame[frame["period_end"] == prior_period]
        prior_row = prior.iloc[-1] if not prior.empty else None

        red_flags: list[str] = []
        verdict = "in_line"
        thesis_impact = "intact"
        quality = 5

        if prior_row is not None:
            if row.net_profit_cr is not None and prior_row["net_profit_cr"] is not None:
                if prior_row["net_profit_cr"] and row.net_profit_cr / prior_row["net_profit_cr"] - 1 < -0.10:
                    verdict = "miss"
                    thesis_impact = "weakens"
                    red_flags.append("net profit down >10% YoY")
                elif prior_row["net_profit_cr"] and row.net_profit_cr / prior_row["net_profit_cr"] - 1 > 0.10:
                    verdict = "beat"
                    thesis_impact = "intact"
            if (
                row.opm_pct is not None
                and prior_row["opm_pct"] is not None
                and (row.opm_pct - prior_row["opm_pct"]) * 100 <= -200
            ):
                red_flags.append("OPM compressed >200bps YoY")
                if thesis_impact == "intact":
                    thesis_impact = "weakens"
            if (
                row.sales_cr is not None
                and prior_row["sales_cr"] is not None
                and prior_row["sales_cr"]
                and row.sales_cr / prior_row["sales_cr"] - 1 < 0
                and verdict == "miss"
            ):
                thesis_impact = "broken"

        if red_flags and thesis_impact == "broken":
            quality = 2
        elif verdict == "beat":
            quality = 7
        elif verdict == "miss":
            quality = 4

        rationale = (
            f"[rules_only] Latest quarter vs same quarter last year: "
            f"verdict {verdict}, thesis {thesis_impact}."
            + (f" Flags: {'; '.join(red_flags)}." if red_flags else "")
        )
        return EarningsAnalysis(
            symbol=symbol,
            period_end=period_end,
            verdict=verdict,
            quality_of_earnings=quality,
            red_flags=red_flags,
            thesis_impact=thesis_impact,
            rationale=rationale,
            confidence=0.4,
            generated_by_llm=False,
            brief_excerpt=brief[:500],
        )
