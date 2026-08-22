"""Optional Kronos sequence-model wrapper for intraday ranking."""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Tuple

import pandas as pd
import requests

from src.config import Config
from src.intraday.kronos_data import KronosWindow, build_kronos_windows
from src.intraday.kronos_features import build_kronos_market_features
from src.intraday.models import RankedStock
from src.logging_setup import get_logger

logger = get_logger(__name__)

_SERVICE_CACHE: Dict[Tuple[str, str, str, str, str, str], "KronosService"] = {}


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


class _KronosBackend:
    def load(self) -> Optional[object]:
        raise NotImplementedError

    def predict_windows(
        self,
        windows: Dict[str, KronosWindow],
    ) -> Dict[str, KronosForecastResult]:
        raise NotImplementedError


class _LocalKronosBackend(_KronosBackend):
    def __init__(self, config: Config):
        self._config = config
        self._predictor = None
        self._load_attempted = False
        self._load_error: Optional[str] = None

    @property
    def load_error(self) -> Optional[str]:
        return self._load_error

    def _device(self) -> str:
        override = (os.getenv("KRONOS_DEVICE") or "").strip()
        if override:
            return override
        return str(self._config.get("intraday.kronos.device", "cpu") or "cpu")

    def _cache_dir(self) -> str:
        override = (os.getenv("KRONOS_HF_CACHE_DIR") or "").strip()
        if override:
            return override
        return str(self._config.get("intraday.kronos.hf_cache_dir", "") or "").strip()

    def load(self):
        if self._load_attempted:
            return self._predictor
        self._load_attempted = True
        started = time.perf_counter()
        try:
            repo_path = str(self._config.get("intraday.kronos.repo_path", "") or "").strip()
            if repo_path:
                candidate = Path(repo_path)
                if not candidate.is_absolute():
                    candidate = self._config.root / candidate
                if candidate.exists():
                    sys.path.insert(0, str(candidate))
            from model import Kronos, KronosPredictor, KronosTokenizer  # type: ignore

            tokenizer_id = str(
                self._config.get(
                    "intraday.kronos.tokenizer_id",
                    "NeoQuasar/Kronos-Tokenizer-base",
                )
            )
            model_id = str(
                self._config.get("intraday.kronos.model_id", "NeoQuasar/Kronos-small")
            )
            device = self._device()
            max_context = max(int(self._config.get("intraday.kronos.max_context", 512) or 512), 8)
            cache_dir = self._cache_dir()
            cache_path = None
            if cache_dir:
                cache_path = Path(cache_dir)
                if not cache_path.is_absolute():
                    cache_path = self._config.root / cache_path
                cache_path.mkdir(parents=True, exist_ok=True)

            load_kwargs = {"cache_dir": str(cache_path)} if cache_path is not None else {}
            tokenizer = KronosTokenizer.from_pretrained(tokenizer_id, **load_kwargs)
            model = Kronos.from_pretrained(model_id, **load_kwargs)
            self._predictor = KronosPredictor(
                model,
                tokenizer,
                device=device,
                max_context=max_context,
            )
            logger.info(
                "Kronos local backend ready for %s in %.2fs (model=%s device=%s)",
                self._config.persona_id,
                time.perf_counter() - started,
                model_id,
                device,
            )
        except Exception as exc:
            self._load_error = str(exc)
            logger.warning("Kronos unavailable for %s: %s", self._config.persona_id, exc)
            self._predictor = None
        return self._predictor

    def predict_windows(
        self,
        windows: Dict[str, KronosWindow],
    ) -> Dict[str, KronosForecastResult]:
        predictor = self.load()
        if predictor is None:
            model_version = str(
                self._config.get("intraday.kronos.model_id", "NeoQuasar/Kronos-small")
            )
            mode = str(self._config.get("intraday.kronos.mode", "shadow") or "shadow")
            return {
                symbol: KronosForecastResult(
                    symbol=symbol,
                    available=False,
                    mode=mode,
                    model_version=model_version,
                    rationale=self._load_error or "kronos predictor unavailable",
                )
                for symbol in windows
            }
        ordered = list(windows.values())
        started = time.perf_counter()
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
            model_version = str(
                self._config.get("intraday.kronos.model_id", "NeoQuasar/Kronos-small")
            )
            mode = str(self._config.get("intraday.kronos.mode", "shadow") or "shadow")
            return {
                window.symbol: KronosForecastResult(
                    symbol=window.symbol,
                    available=False,
                    mode=mode,
                    model_version=model_version,
                    rationale=f"kronos prediction failed: {exc}",
                )
                for window in ordered
            }
        duration = time.perf_counter() - started
        if ordered:
            logger.info(
                "Kronos local batch scored %d window(s) for %s in %.2fs",
                len(ordered),
                self._config.persona_id,
                duration,
            )
        return _forecast_results_from_frames(
            ordered,
            forecasts,
            mode=str(self._config.get("intraday.kronos.mode", "shadow") or "shadow"),
            model_version=str(
                self._config.get("intraday.kronos.model_id", "NeoQuasar/Kronos-small")
            ),
            source="kronos_local",
        )


class _RemoteKronosBackend(_KronosBackend):
    def __init__(self, config: Config):
        self._config = config
        self._endpoint = (os.getenv("KRONOS_ENDPOINT_URL") or "").strip() or str(
            self._config.get("intraday.kronos.endpoint_url", "") or ""
        ).strip()
        self._timeout = float(self._config.get("intraday.kronos.request_timeout_seconds", 15.0) or 15.0)
        self._load_error: Optional[str] = None

    @property
    def load_error(self) -> Optional[str]:
        return self._load_error

    def load(self) -> Optional[object]:
        if not self._endpoint:
            self._load_error = "kronos remote endpoint_url is not configured"
            return None
        return object()

    def predict_windows(
        self,
        windows: Dict[str, KronosWindow],
    ) -> Dict[str, KronosForecastResult]:
        if self.load() is None:
            model_version = str(
                self._config.get("intraday.kronos.model_id", "NeoQuasar/Kronos-small")
            )
            mode = str(self._config.get("intraday.kronos.mode", "shadow") or "shadow")
            return {
                symbol: KronosForecastResult(
                    symbol=symbol,
                    available=False,
                    mode=mode,
                    model_version=model_version,
                    rationale=self._load_error or "kronos remote predictor unavailable",
                )
                for symbol in windows
            }

        ordered = list(windows.values())
        payload = {
            "persona_id": self._config.persona_id,
            "model_id": str(self._config.get("intraday.kronos.model_id", "NeoQuasar/Kronos-small")),
            "tokenizer_id": str(
                self._config.get("intraday.kronos.tokenizer_id", "NeoQuasar/Kronos-Tokenizer-base")
            ),
            "device": str(self._config.get("intraday.kronos.device", "cpu") or "cpu"),
            "pred_len": ordered[0].pred_len if ordered else 0,
            "temperature": float(self._config.get("intraday.kronos.temperature", 1.0) or 1.0),
            "top_p": float(self._config.get("intraday.kronos.top_p", 0.9) or 0.9),
            "sample_count": int(self._config.get("intraday.kronos.sample_count", 1) or 1),
            "windows": [_serialize_window(window) for window in ordered],
        }
        started = time.perf_counter()
        try:
            response = requests.post(
                self._endpoint.rstrip("/") + "/predict",
                json=payload,
                timeout=self._timeout,
            )
            response.raise_for_status()
            body = response.json()
            if not isinstance(body, dict):
                raise ValueError("remote response must be a JSON object")
            raw_forecasts = body.get("forecasts")
            if not isinstance(raw_forecasts, list):
                raise ValueError("remote response missing forecasts list")
            if len(raw_forecasts) != len(ordered):
                raise ValueError(
                    "remote response forecast count mismatch: "
                    f"expected {len(ordered)}, received {len(raw_forecasts)}"
                )
            forecasts = [_deserialize_forecast(item) for item in raw_forecasts]
        except Exception as exc:
            logger.warning("Kronos remote prediction failed: %s", exc)
            model_version = str(
                self._config.get("intraday.kronos.model_id", "NeoQuasar/Kronos-small")
            )
            mode = str(self._config.get("intraday.kronos.mode", "shadow") or "shadow")
            return {
                window.symbol: KronosForecastResult(
                    symbol=window.symbol,
                    available=False,
                    mode=mode,
                    model_version=model_version,
                    rationale=f"kronos remote prediction failed: {exc}",
                    source="kronos_remote",
                )
                for window in ordered
            }
        duration = time.perf_counter() - started
        logger.info(
            "Kronos remote batch scored %d window(s) for %s in %.2fs via %s",
            len(ordered),
            self._config.persona_id,
            duration,
            self._endpoint,
        )
        return _forecast_results_from_frames(
            ordered,
            forecasts,
            mode=str(self._config.get("intraday.kronos.mode", "shadow") or "shadow"),
            model_version=str(
                self._config.get("intraday.kronos.model_id", "NeoQuasar/Kronos-small")
            ),
            source="kronos_remote",
        )


class KronosService:
    def __init__(self, config: Config):
        self._config = config
        backend = self.backend_name
        self._backend_name = backend if backend in {"local", "http", "remote"} else "local"
        self._backend: _KronosBackend = (
            _RemoteKronosBackend(config) if self._backend_name in {"http", "remote"} else _LocalKronosBackend(config)
        )

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

    @property
    def backend_name(self) -> str:
        override = (os.getenv("KRONOS_BACKEND") or "").strip().lower()
        if override in {"local", "http", "remote"}:
            return override
        backend = str(self._config.get("intraday.kronos.backend", "local") or "local").lower()
        return backend if backend in {"local", "http", "remote"} else "local"

    def score_frames(self, frames: Dict[str, pd.DataFrame]) -> Dict[str, KronosForecastResult]:
        if not self.enabled:
            return {}
        windows = build_kronos_windows(self._config, frames)
        if not windows:
            return {}
        return self._backend.predict_windows(windows)


def get_kronos_service(config: Config) -> KronosService:
    key = (
        config.persona_id,
        str(config.get("intraday.kronos.model_id", "NeoQuasar/Kronos-small")),
        str(config.get("intraday.kronos.tokenizer_id", "NeoQuasar/Kronos-Tokenizer-base")),
        str(config.get("intraday.kronos.mode", "shadow")),
        str(os.getenv("KRONOS_DEVICE") or config.get("intraday.kronos.device", "cpu")),
        str(config.get("intraday.kronos.repo_path", "")),
        str(os.getenv("KRONOS_BACKEND") or config.get("intraday.kronos.backend", "local")),
        str(os.getenv("KRONOS_ENDPOINT_URL") or config.get("intraday.kronos.endpoint_url", "")),
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


def _forecast_results_from_frames(
    windows: list[KronosWindow],
    forecasts: list[pd.DataFrame],
    *,
    mode: str,
    model_version: str,
    source: str,
) -> Dict[str, KronosForecastResult]:
    results: Dict[str, KronosForecastResult] = {}
    for window, forecast in zip(windows, forecasts):
        features = build_kronos_market_features(window.history, forecast)
        results[window.symbol] = KronosForecastResult(
            symbol=window.symbol,
            available=True,
            mode=mode,
            model_version=model_version,
            quality_score=float(features["quality_score"]),
            expected_return_pct=float(features["expected_return_pct"]),
            expected_range_pct=float(features["expected_range_pct"]),
            confidence=float(features["confidence"]),
            trend_bias=str(features["trend_bias"]),
            rationale=str(features["rationale"]),
            source=source,
        )
    return results


def _serialize_window(window: KronosWindow) -> Dict[str, object]:
    frame = window.history.reset_index()
    ts_column = str(frame.columns[0])
    frame = frame.rename(columns={ts_column: "timestamp"})
    frame["timestamp"] = frame["timestamp"].astype(str)
    return {
        "symbol": window.symbol,
        "history": frame.to_dict(orient="records"),
        "x_timestamp": [str(ts) for ts in window.x_timestamp.tolist()],
        "y_timestamp": [str(ts) for ts in window.y_timestamp.tolist()],
        "pred_len": window.pred_len,
        "truncated": window.truncated,
    }


def _deserialize_forecast(payload: object) -> pd.DataFrame:
    if not isinstance(payload, dict):
        raise ValueError("remote forecast payload must be a mapping")
    rows = payload.get("forecast")
    if not isinstance(rows, list):
        raise ValueError("remote forecast payload missing forecast rows")
    frame = pd.DataFrame(rows)
    if "timestamp" in frame.columns:
        frame["timestamp"] = pd.to_datetime(frame["timestamp"])
        frame = frame.set_index("timestamp")
    return frame
