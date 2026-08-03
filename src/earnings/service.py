"""Orchestrates earnings polling, analysis, digest, and triggers."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Set

from src.config import Config, get_config
from src.earnings.analyzer import EarningsAnalyzer
from src.earnings.brief import latest_period
from src.earnings.ingest import EarningsIngestor
from src.earnings.triggers import EarningsTriggerEngine
from src.logging_setup import get_logger
from src.models import EarningsAnalysis, EarningsDigest, PortfolioState, Trigger
from src.universe import Universe, load_universe

logger = get_logger(__name__)

# Indian fiscal quarters end Mar/Jun/Sep/Dec; results season follows.
_QUARTER_END_MONTHS = (3, 6, 9, 12)


class EarningsService:
    """Poll quarterly results and analyse fresh prints for the daily cycle."""

    def __init__(
        self,
        config: Optional[Config] = None,
        universe: Optional[Universe] = None,
    ):
        self._config = config or get_config()
        self._universe = universe or load_universe(self._config)
        self._ingestor = EarningsIngestor(self._config)
        self._analyzer = EarningsAnalyzer(self._config)
        self._triggers = EarningsTriggerEngine(self._config)
        self._state_dir = self._config.cache_dir / "earnings" / "state"
        self._state_dir.mkdir(parents=True, exist_ok=True)

    @property
    def enabled(self) -> bool:
        return bool(self._config.get("earnings.enabled", False))

    def _state_path(self, symbol: str) -> Path:
        return self._state_dir / f"{symbol.upper()}.json"

    def _last_analyzed_period(self, symbol: str) -> Optional[date]:
        path = self._state_path(symbol)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            value = payload.get("period_end")
            return date.fromisoformat(value) if value else None
        except (json.JSONDecodeError, ValueError, OSError):
            return None

    def _mark_analyzed(self, analysis: EarningsAnalysis) -> None:
        payload = {
            "symbol": analysis.symbol,
            "period_end": analysis.period_end.isoformat(),
            "verdict": analysis.verdict,
            "thesis_impact": analysis.thesis_impact,
        }
        self._state_path(analysis.symbol).write_text(
            json.dumps(payload), encoding="utf-8"
        )

    @staticmethod
    def is_results_season(as_of: date, window_days: int = 60) -> bool:
        if as_of.month in (1, 4, 7, 10):
            return True
        if as_of.month in _QUARTER_END_MONTHS and as_of.day <= window_days:
            return True
        prev_month = as_of.month - 1 or 12
        if prev_month in _QUARTER_END_MONTHS:
            return True
        return False

    def symbols_to_poll(
        self,
        portfolio: PortfolioState,
        candidate_symbols: List[str],
    ) -> List[str]:
        symbols: List[str] = []
        if self._config.get("earnings.poll_holdings", True):
            symbols.extend(portfolio.symbols)
        if self._config.get("earnings.poll_candidates", True):
            limit = int(self._config.get("earnings.max_candidates", 10) or 10)
            symbols.extend(candidate_symbols[:limit])
        return list(dict.fromkeys(symbols))

    def build(
        self,
        portfolio: PortfolioState,
        candidate_symbols: List[str],
        as_of: Optional[date] = None,
    ) -> Optional[EarningsDigest]:
        if not self.enabled:
            return None

        as_of = as_of or portfolio.as_of
        window = int(self._config.get("earnings.results_window_days", 60) or 60)
        if not self.is_results_season(as_of, window_days=window):
            logger.info("Outside earnings results season; skipping poll")
            return None

        symbols = self.symbols_to_poll(portfolio, candidate_symbols)
        if not symbols:
            return None

        logger.info("Polling quarterly results for %d symbol(s)", len(symbols))
        polled = self._ingestor.poll_many(symbols)

        analyses: Dict[str, EarningsAnalysis] = {}
        holdings_notes: Dict[str, str] = {}
        watchlist_notes: Dict[str, str] = {}
        flagged: List[str] = []
        new_reports = 0

        for symbol, result in polled.items():
            period = latest_period(result.quarters)
            if period is None:
                continue
            if self._last_analyzed_period(symbol) == period:
                continue

            analysis = self._analyzer.analyze(
                symbol=symbol,
                company_name=self._universe.company_name_of(symbol),
                sector=self._universe.sector_of(symbol),
                quarters=result.quarters,
                period_end=period,
            )
            self._mark_analyzed(analysis)
            analyses[symbol] = analysis
            new_reports += 1

            note = (
                f"{analysis.verdict}/{analysis.thesis_impact}: "
                f"{analysis.rationale[:280]}"
            )
            if symbol in portfolio.symbols:
                holdings_notes[symbol] = note
            else:
                watchlist_notes[symbol] = note

            if analysis.thesis_impact in ("broken", "weakens") or analysis.verdict == "miss":
                flagged.append(symbol)

        digest = EarningsDigest(
            as_of=as_of,
            analyses=analyses,
            holdings_notes=holdings_notes,
            watchlist_notes=watchlist_notes,
            flagged_symbols=sorted(set(flagged)),
            symbols_polled=len(polled),
            new_reports=new_reports,
        )
        logger.info(
            "Earnings digest: %d new report(s) analysed from %d polled",
            new_reports,
            len(polled),
        )
        return digest if digest.analyses else digest

    def triggers_for(
        self,
        digest: Optional[EarningsDigest],
        portfolio: PortfolioState,
        candidate_symbols: List[str],
        is_deep_review: bool,
    ) -> List[Trigger]:
        if digest is None or not digest.analyses:
            return []
        return self._triggers.evaluate(
            digest,
            portfolio,
            candidate_symbols=set(candidate_symbols),
            is_deep_review=is_deep_review,
        )

    def close(self) -> None:
        self._ingestor.close()
