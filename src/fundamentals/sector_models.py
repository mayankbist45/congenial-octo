"""Sector-aware analytics helpers.

These helpers map broad exchange sectors into the more specific analytical
modules that long-horizon personas use. They also centralize which extra
metrics matter for each module so scoring, briefings, and quarterly reviews do
not drift apart.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from src.models import FundamentalSnapshot

SECTOR_MODEL_MAP = {
    "financial services": "banks",
    "insurance": "insurance",
    "information technology": "saas",
    "services": "retail",
    "consumer services": "retail",
    "capital goods": "manufacturing",
    "metals & mining": "commodities",
    "oil gas & consumable fuels": "commodities",
    "realty": "real_estate",
    "telecommunication": "telecom",
}

SECTOR_MODEL_METRICS: Dict[str, List[str]] = {
    "banks": [
        "nim",
        "gnpa",
        "nnpa",
        "credit_cost",
        "casa",
        "roa",
        "cet1",
        "loan_growth",
    ],
    "insurance": [
        "ape",
        "vnb_margin",
        "persistency",
        "solvency",
        "claims_ratio",
    ],
    "saas": [
        "arr",
        "nrr",
        "churn",
        "cac_payback",
        "rule_of_40",
        "remaining_obligations",
    ],
    "retail": [
        "same_store_sales",
        "store_economics",
        "inventory_turns",
    ],
    "manufacturing": [
        "capacity_utilisation",
        "order_book",
        "input_cost_index",
        "cost_curve_position",
    ],
    "commodities": [
        "realised_price",
        "production_cost",
        "reserves",
        "hedging_ratio",
    ],
    "real_estate": [
        "pre_sales",
        "collections",
        "inventory",
        "net_debt",
        "project_pipeline",
    ],
    "telecom": [
        "arpu",
        "churn",
        "subscribers",
        "spectrum_liabilities",
        "capex_intensity",
    ],
}

SECTOR_MACRO_EXPOSURES: Dict[str, Dict[str, float]] = {
    "banks": {"rates": 1.0, "credit": 1.0, "domestic_demand": 0.7},
    "insurance": {"rates": 0.8, "domestic_demand": 0.6},
    "saas": {"fx": 0.9, "global_growth": 0.7},
    "retail": {"inflation": 0.7, "domestic_demand": 1.0},
    "manufacturing": {"commodities": 0.7, "industrial_production": 0.9},
    "commodities": {"commodities": 1.0, "fx": 0.6, "global_growth": 0.8},
    "real_estate": {"rates": 1.0, "domestic_demand": 0.8, "credit": 0.8},
    "telecom": {"rates": 0.6, "domestic_demand": 0.7},
}


def infer_sector_model(sector: Optional[str]) -> Optional[str]:
    if not sector:
        return None
    cleaned = " ".join(str(sector).split()).lower()
    return SECTOR_MODEL_MAP.get(cleaned)


def supported_sector_metrics(sector_model: Optional[str]) -> List[str]:
    if not sector_model:
        return []
    return list(SECTOR_MODEL_METRICS.get(sector_model, []))


def apply_sector_context(
    snapshot: FundamentalSnapshot,
    sector: Optional[str],
) -> FundamentalSnapshot:
    """Backfill sector-derived metadata without inventing unavailable data."""
    sector_model = snapshot.sector_model or infer_sector_model(sector)
    snapshot.sector_model = sector_model
    if not snapshot.macro_exposures and sector_model:
        snapshot.macro_exposures = dict(SECTOR_MACRO_EXPOSURES.get(sector_model, {}))

    supported = set(supported_sector_metrics(sector_model))
    if supported and snapshot.sector_metrics:
        for metric in supported:
            snapshot.sector_metrics.setdefault(metric, None)
    elif supported and not snapshot.sector_metrics:
        snapshot.sector_metrics = {metric: None for metric in supported}

    unsupported = [
        metric
        for metric in supported_sector_metrics(sector_model)
        if snapshot.sector_metrics.get(metric) is None
    ]
    if unsupported:
        snapshot.unsupported_metrics = sorted(set(snapshot.unsupported_metrics + unsupported))
    return snapshot

