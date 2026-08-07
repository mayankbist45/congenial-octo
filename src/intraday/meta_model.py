"""Lightweight binary meta-label classifier (take / skip)."""

from __future__ import annotations

import json
import pickle
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from src.config import Config
from src.logging_setup import get_logger

logger = get_logger(__name__)

# Numeric features used by the sklearn model (stable order).
NUMERIC_FEATURES = (
    "hour_of_day",
    "gap_pct",
    "trend_cleanliness",
    "volume_expansion",
    "vwap_distance_pct",
    "prior_level_distance_pct",
    "range_pct",
    "rank_score",
    "in_play",
    "gross_rr",
    "estimated_costs",
    "pe_ratio",
    "peg_ratio",
    "earnings_growth_pct",
    "revenue_growth_pct",
    "watch_confidence",
)

STRATEGY_VOCAB = (
    "scalping",
    "opening_range_breakout",
    "vwap_pullback",
    "vwap_fade",
    "opening_gap_fade",
    "range_revert",
    "ofi_momentum",
    "ofi_exhaustion",
    "stat_arb_pairs",
    "cross_stat_arb",
    "other",
)

SIDE_VOCAB = ("BUY", "SELL")
STYLE_VOCAB = ("growth", "value", "neutral", "value_trap", "unknown", "other")


@dataclass
class MetaScore:
    take: bool
    confidence: float
    method: str = "model"
    rationale: str = ""
    model_version: str = ""


class MetaLabelModel:
    """Logistic-regression take/skip model with pickle persistence."""

    def __init__(self, pipeline: Any = None, version: str = "untrained"):
        self._pipeline = pipeline
        self.version = version

    @property
    def ready(self) -> bool:
        return self._pipeline is not None

    def predict(self, features: Dict[str, Any]) -> MetaScore:
        if self._pipeline is None:
            return MetaScore(
                take=True,
                confidence=0.5,
                method="untrained",
                rationale="no meta model loaded",
                model_version=self.version,
            )
        x = np.asarray([self.vectorize(features)], dtype=float)
        proba = float(self._pipeline.predict_proba(x)[0][1])
        take = proba >= 0.5
        return MetaScore(
            take=take,
            confidence=round(proba, 4),
            method="model",
            rationale=f"meta_model p(take)={proba:.3f}",
            model_version=self.version,
        )

    def should_take(
        self,
        features: Dict[str, Any],
        *,
        min_confidence: float = 0.55,
    ) -> MetaScore:
        score = self.predict(features)
        if score.method == "untrained":
            return score
        score.take = score.confidence >= min_confidence
        if not score.take:
            score.rationale = (
                f"meta_model p(take)={score.confidence:.3f} "
                f"< min_confidence={min_confidence:.2f}"
            )
        return score

    @staticmethod
    def vectorize(features: Dict[str, Any]) -> List[float]:
        row: List[float] = []
        for key in NUMERIC_FEATURES:
            if key == "watch_confidence":
                val = features.get("watch_confidence", features.get("confidence"))
            else:
                val = features.get(key)
            if key == "in_play":
                row.append(1.0 if val else 0.0)
            elif val is None:
                row.append(0.0)
            else:
                try:
                    row.append(float(val))
                except (TypeError, ValueError):
                    row.append(0.0)
        strategy = str(features.get("strategy") or "other")
        if strategy not in STRATEGY_VOCAB:
            strategy = "other"
        for name in STRATEGY_VOCAB:
            row.append(1.0 if strategy == name else 0.0)
        side = str(features.get("side") or "BUY").upper()
        for name in SIDE_VOCAB:
            row.append(1.0 if side == name else 0.0)
        style = str(features.get("fundamental_style") or "unknown")
        if style not in STYLE_VOCAB:
            style = "other"
        for name in STYLE_VOCAB:
            row.append(1.0 if style == name else 0.0)
        return row

    @classmethod
    def train(
        cls,
        rows: Sequence[Dict[str, Any]],
        *,
        version: Optional[str] = None,
    ) -> Tuple["MetaLabelModel", Dict[str, Any]]:
        """Train logistic regression. Each row needs features + meta_label (0/1)."""
        from sklearn.linear_model import LogisticRegression
        from sklearn.pipeline import Pipeline
        from sklearn.preprocessing import StandardScaler

        labeled = [r for r in rows if r.get("meta_label") in (0, 1)]
        if len(labeled) < 10:
            raise ValueError(
                f"Need at least 10 labeled rows to train; got {len(labeled)}"
            )
        x = np.asarray(
            [cls.vectorize(r.get("features") or r) for r in labeled], dtype=float
        )
        y = np.asarray([int(r["meta_label"]) for r in labeled], dtype=int)
        if len(set(y.tolist())) < 2:
            # Degenerate: all wins or all losses — still fit a weak prior.
            y = y.copy()
            y[0] = 1 - y[0]

        pipeline = Pipeline(
            [
                ("scaler", StandardScaler()),
                (
                    "clf",
                    LogisticRegression(
                        max_iter=500,
                        class_weight="balanced",
                        solver="lbfgs",
                    ),
                ),
            ]
        )
        pipeline.fit(x, y)
        pred = pipeline.predict(x)
        acc = float((pred == y).mean()) if len(y) else 0.0
        pos_rate = float(y.mean())
        ver = version or datetime.now().strftime("v%Y%m%d_%H%M%S")
        metrics = {
            "n_samples": len(labeled),
            "pos_rate": round(pos_rate, 3),
            "train_accuracy": round(acc, 3),
            "version": ver,
        }
        logger.info(
            "Trained meta model %s on %d rows (pos_rate=%.2f acc=%.2f)",
            ver,
            len(labeled),
            pos_rate,
            acc,
        )
        return cls(pipeline, version=ver), metrics

    def save(self, path: Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": self.version,
            "pipeline": self._pipeline,
            "numeric_features": NUMERIC_FEATURES,
        }
        with open(path, "wb") as handle:
            pickle.dump(payload, handle, protocol=pickle.HIGHEST_PROTOCOL)
        meta_path = path.with_suffix(".json")
        meta_path.write_text(
            json.dumps({"version": self.version, "path": str(path)}, indent=2),
            encoding="utf-8",
        )
        return path

    @classmethod
    def load(cls, path: Path) -> "MetaLabelModel":
        with open(path, "rb") as handle:
            payload = pickle.load(handle)
        if isinstance(payload, dict):
            return cls(payload.get("pipeline"), version=str(payload.get("version", "loaded")))
        return cls(payload, version="loaded")


def default_model_path(config: Config) -> Path:
    configured = config.get("intraday.meta.model_path")
    if configured:
        return config.path("intraday.meta.model_path")
    return config.root / "data" / "intraday" / f"meta_model_{config.persona_id}.pkl"


def load_meta_model(config: Config) -> Optional[MetaLabelModel]:
    path = default_model_path(config)
    if not path.exists():
        return None
    try:
        return MetaLabelModel.load(path)
    except Exception as exc:
        logger.warning("Could not load meta model from %s: %s", path, exc)
        return None
