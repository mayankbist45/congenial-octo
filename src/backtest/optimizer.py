"""Parameter optimizer for persona strategies.

Performs grid search over key tunable persona parameters to find the
combination that maximizes a target performance metric (e.g. Sharpe ratio).
"""

from __future__ import annotations

import copy
import itertools
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from src.backtest.engine import BacktestEngine, BacktestResult
from src.config import Config, get_persona_config
from src.logging_setup import get_logger

logger = get_logger(__name__)


@dataclass
class OptimizationResult:
    persona_id: str
    original_params: Dict[str, Any]
    optimized_params: Dict[str, Any]
    original_performance: BacktestResult
    optimized_performance: BacktestResult
    improvements: Dict[str, float] = field(default_factory=dict)


def _frange(start: float, stop: float, step: float) -> List[float]:
    """Generate a list of floats from start to stop (inclusive) by step."""
    values: List[float] = []
    current = start
    while current <= stop + step * 0.01:
        values.append(round(current, 4))
        current += step
    return values


class PersonaOptimizer:
    """Grid search over key persona parameters to find better settings."""

    TUNABLE_PARAMS: Dict[str, Tuple[float, float, float]] = {
        "risk.stop_loss_pct": (-25.0, -10.0, 5.0),
        "risk.trailing_stop_pct": (-30.0, -10.0, 5.0),
        "risk.profit_review_pct": (20.0, 60.0, 10.0),
        "quant.min_buy_score": (0.2, 0.8, 0.1),
        "fund.target_positions": (10, 30, 5),
    }

    def __init__(
        self,
        persona_id: str,
        start_date: date,
        end_date: date,
        params: Optional[Dict[str, Tuple[float, float, float]]] = None,
    ):
        self._persona_id = persona_id
        self._start_date = start_date
        self._end_date = end_date
        self._params = params or self.TUNABLE_PARAMS
        self._base_config = get_persona_config(persona_id)

    def optimize(self, metric: str = "sharpe_ratio") -> OptimizationResult:
        """Find the parameter combination that maximizes the target metric."""
        logger.info(
            "Starting optimization for persona=%s, metric=%s, period=%s to %s",
            self._persona_id, metric, self._start_date, self._end_date,
        )

        # Run baseline.
        original_params = self._extract_current_params()
        logger.info("Original params: %s", original_params)

        baseline = self._run_backtest(self._base_config)
        baseline_metric = self._extract_metric(baseline, metric)
        logger.info(
            "Baseline %s = %s",
            metric,
            f"{baseline_metric:.4f}" if baseline_metric is not None else "N/A",
        )

        # Generate parameter grid.
        param_names = list(self._params.keys())
        param_ranges: List[List[float]] = []
        for name in param_names:
            start, stop, step = self._params[name]
            param_ranges.append(_frange(start, stop, step))

        combinations = list(itertools.product(*param_ranges))
        total = len(combinations)
        logger.info(
            "Grid search: %d parameters, %d combinations",
            len(param_names), total,
        )

        best_metric = baseline_metric
        best_params = dict(original_params)
        best_result = baseline

        started = time.time()
        for idx, combo in enumerate(combinations, start=1):
            trial_params = dict(zip(param_names, combo))

            config = self._make_config(trial_params)
            result = self._run_backtest(config)
            trial_metric = self._extract_metric(result, metric)

            if trial_metric is not None and (
                best_metric is None or trial_metric > best_metric
            ):
                best_metric = trial_metric
                best_params = trial_params
                best_result = result
                logger.info(
                    "  [%d/%d] New best %s=%.4f with %s",
                    idx, total, metric, best_metric, trial_params,
                )

            if idx % 10 == 0 or idx == total:
                elapsed = time.time() - started
                rate = idx / max(elapsed, 0.001)
                eta = (total - idx) / rate if rate > 0 else 0
                logger.info(
                    "  Progress: %d/%d (%.1f/s, ETA %.0fs) | best %s=%.4f",
                    idx, total, rate, eta, metric,
                    best_metric if best_metric is not None else 0.0,
                )

        # Compute improvements.
        improvements: Dict[str, float] = {}
        for attr in ("total_return_pct", "annualised_return_pct", "max_drawdown_pct",
                     "sharpe_ratio", "sortino_ratio"):
            orig_val = getattr(baseline, attr, None)
            best_val = getattr(best_result, attr, None)
            if orig_val is not None and best_val is not None:
                improvements[attr] = best_val - orig_val

        logger.info(
            "Optimization complete. Best %s=%.4f (was %s). Improvements: %s",
            metric,
            best_metric if best_metric is not None else 0.0,
            f"{baseline_metric:.4f}" if baseline_metric is not None else "N/A",
            {k: f"{v:+.2f}" for k, v in improvements.items()},
        )

        return OptimizationResult(
            persona_id=self._persona_id,
            original_params=original_params,
            optimized_params=best_params,
            original_performance=baseline,
            optimized_performance=best_result,
            improvements=improvements,
        )

    def _extract_current_params(self) -> Dict[str, Any]:
        """Read the current values of tunable parameters from the config."""
        params: Dict[str, Any] = {}
        for name in self._params:
            params[name] = self._base_config.get(name)
        return params

    def _make_config(self, params: Dict[str, Any]) -> Config:
        """Create a Config variant with overridden parameters."""
        raw = copy.deepcopy(self._base_config.raw)
        for dotted_path, value in params.items():
            parts = dotted_path.split(".")
            node = raw
            for part in parts[:-1]:
                if part not in node or not isinstance(node[part], dict):
                    node[part] = {}
                node = node[part]
            node[parts[-1]] = value

        return Config(
            raw=raw,
            secrets=self._base_config.secrets,
            root=self._base_config.root,
            persona_id=self._base_config.persona_id,
        )

    def _run_backtest(self, config: Config) -> BacktestResult:
        """Run a single backtest with the given configuration."""
        engine = BacktestEngine(
            config=config,
            start_date=self._start_date,
            end_date=self._end_date,
        )
        return engine.run()

    @staticmethod
    def _extract_metric(result: BacktestResult, metric: str) -> Optional[float]:
        """Get a named metric from a BacktestResult."""
        value = getattr(result, metric, None)
        if value is None:
            return None
        return float(value)
