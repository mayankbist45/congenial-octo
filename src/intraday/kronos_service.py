"""Optional Kronos sequence-model wrapper for intraday ranking."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Tuple

import pandas as pd

from src.config import Config
from src.intraday.kronos_data import KronosWindow, build_kronos_windows
from src.intraday.kronos_features import build_kronos_market_features
from src.intraday.models import RankedStock
from src.logging_setup import get_logger

logger = get_logger(__name__)

_SERVICE_CACHE: Dict[Tuple[str, str, str, str], "KronosService"] = {}


@dataclass
class KronosForecastResult:
    symbol: str
    available: bool
    mode: str
    model_version: str
    quality_score: Optional[float] = None
    expected_return_pct: Optional[float] = None
    expected_range_pct: Optional[float] = None
    confidence: Optional[float] = None
    trend_bias: Optional[str] = None
    rationale: str = ""
    source: str = "unavailable"


class KronosService:
    def __init__(self, config: Config):
        self._config = config
        self._predictor = None
        self._load_attempted = False
        self._load_error: Optional[str] = None

    @property
    def enabled(self) -> bool:
        return bool(self._config.get("intraday.kronos.enabled", False))

    @property
    def mode(self) -> str:
        mode = str(self._config.get("intraday.kronos.mode", "shadow") or "shadow").lower()
        return mode if mode in {"shadow", "advisory"} else "shadow"

    @property
    def model_version(self) -> str:
        return str(self._config.get("intraday.kronos.model_id", "NeoQuasar/Kronos-small"))

    def score_frames(self, frames: Dict[str, pd.DataFrame]) -> Dict[str, KronosForecastResult]:
        if not self.enabled:
            return {}
        windows = build_kronos_windows(self._config, frames)
        if not windows:
            return {}
        predictor = self._ensure_predictor()
        if predictor is None:
            return {
                symbol: KronosForecastResult(
                    symbol=symbol,
                    available=False,
                    mode=self.mode,
                    model_version=self.model_version,
                    rationale=self._load_error or "kronos predictor unavailable",
                )
                for symbol in windows
            }
        return self._predict_windows(predictor, windows)

    def _ensure_predictor(self):
        if self._load_attempted:
            return self._predictor
        self._load_attempted = True
        try:
            repo_path = str(self._config.get("intraday.kronos.repo_path", "") or "").strip()
            if repo_path:
                candidate = Path(repo_path)
                if candidate.exists():
                    sys.path.insert(0, str(candidate))
            from model import Kronos, KronosPredictor, KronosTokenizer  # type: ignore

            tokenizer_id = str(
                self._config.get(
                    "intraday.kronos.tokenizer_id",
                    "NeoQuasar/Kronos-Tokenizer-base",
                )
            )
            model_id = self.model_version
            device = str(self._config.get("intraday.kronos.device", "cpu") or "cpu")
            max_context = max(int(self._config.get("intraday.kronos.max_context", 512) or 512), 8)
            tokenizer = KronosTokenizer.from_pretrained(tokenizer_id)
            model = Kronos.from_pretrained(model_id)
            self._predictor = KronosPredictor(
                model,
                tokenizer,
                device=device,
                max_context=max_context,
            )
        except Exception as exc:
            self._load_error = str(exc)
            logger.warning("Kronos unavailable for %s: %s", self._config.persona_id, exc)
            self._predictor = None
        return self._predictor

    def _predict_windows(
        self,
        predictor,
        windows: Dict[str, KronosWindow],
    ) -> Dict[str, KronosForecastResult]:
        ordered = list(windows.values())
        try:
            pred_len = ordered[0].pred_len
            forecasts = predictor.predict_batch(
                df_list=[window.history for window in ordered],
                x_timestamp_list=[window.x_timestamp for window in ordered],
                y_timestamp_list=[window.y_timestamp for window in ordered],
                pred_len=pred_len,
                T=float(self._config.get("intraday.kronos.temperature", 1.0) or 1.0),
                top_p=float(self._config.get("intraday.kronos.top_p", 0.9) or 0.9),
                sample_count=int(self._config.get("intraday.kronos.sample_count", 1) or 1),
                verbose=False,
            )
        except Exception as exc:
            logger.warning("Kronos batch prediction failed: %s", exc)
            return {
                window.symbol: KronosForecastResult(
                    symbol=window.symbol,
                    available=False,
                    mode=self.mode,
                    model_version=self.model_version,
                    rationale=f"kronos prediction failed: {exc}",
                )
                for window in ordered
            }

        results: Dict[str, KronosForecastResult] = {}
        for window, forecast in zip(ordered, forecasts):
            features = build_kronos_market_features(window.history, forecast)
            results[window.symbol] = KronosForecastResult(
                symbol=window.symbol,
                available=True,
                mode=self.mode,
                model_version=self.model_version,
                quality_score=float(features["quality_score"]),
                expected_return_pct=float(features["expected_return_pct"]),
                expected_range_pct=float(features["expected_range_pct"]),
                confidence=float(features["confidence"]),
                trend_bias=str(features["trend_bias"]),
                rationale=str(features["rationale"]),
                source="kronos",
            )
        return results


def get_kronos_service(config: Config) -> KronosService:
    key = (
        config.persona_id,
        str(config.get("intraday.kronos.model_id", "NeoQuasar/Kronos-small")),
        str(config.get("intraday.kronos.tokenizer_id", "NeoQuasar/Kronos-Tokenizer-base")),
        str(config.get("intraday.kronos.mode", "shadow")),
        str(config.get("intraday.kronos.device", "cpu")),
        str(config.get("intraday.kronos.repo_path", "")),
    )
    service = _SERVICE_CACHE.get(key)
    if service is None:
        service = KronosService(config)
        _SERVICE_CACHE[key] = service
    return service


def maybe_enrich_ranked_with_kronos(
    config: Config,
    frames: Dict[str, pd.DataFrame],
    ranked: list[RankedStock],
) -> Dict[str, KronosForecastResult]:
    if not ranked or not bool(config.get("intraday.kronos.enabled", False)):
        return {}
    service = get_kronos_service(config)
    frame_subset = {item.symbol: frames[item.symbol] for item in ranked if item.symbol in frames}
    results = service.score_frames(frame_subset)
    for item in ranked:
        result = results.get(item.symbol)
        if result is None or not result.available:
            continue
        item.kronos_enabled = True
        item.kronos_mode = result.mode
        item.kronos_score = result.quality_score
        item.kronos_expected_return_pct = result.expected_return_pct
        item.kronos_confidence = result.confidence
        item.kronos_trend_bias = result.trend_bias
        item.kronos_rationale = result.rationale
        if result.rationale and result.rationale not in item.reasons:
            item.reasons.append(result.rationale)
    return results
