"""Liquid/overnight fund NAV data from the MFAPI.in free API.

Parks idle cash in a liquid mutual fund to earn overnight returns instead of
letting it sit at zero. The NAV is fetched daily and cached for 12 hours.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from src.config import Config, get_config
from src.logging_setup import get_logger

logger = get_logger(__name__)

_API_BASE = "https://api.mfapi.in/mf"


@dataclass
class LiquidFundNAV:
    scheme_code: str
    scheme_name: str
    nav: float
    date: date
    daily_return_pct: float


class LiquidFundService:
    """Fetches and caches liquid fund NAV data."""

    def __init__(self, config: Optional[Config] = None):
        self._config = config or get_config()
        self._scheme_code = str(
            self._config.get("liquid_fund.scheme_code", "119551")
        )

    @property
    def _cache_path(self) -> Path:
        return self._config.cache_dir / f"liquid_nav_{self._scheme_code}.json"

    def _cache_ttl_seconds(self) -> float:
        hours = float(self._config.get("market_data.cache_ttl_hours", 12) or 12)
        return hours * 3600.0

    def _cache_is_fresh(self) -> bool:
        path = self._cache_path
        if not path.exists():
            return False
        return (time.time() - path.stat().st_mtime) < self._cache_ttl_seconds()

    def _load_cache(self) -> Optional[Dict[str, Any]]:
        try:
            with open(self._cache_path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            return None

    def _save_cache(self, payload: Dict[str, Any]) -> None:
        try:
            with open(self._cache_path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
        except Exception as exc:
            logger.warning("Failed to cache liquid fund data: %s", exc)

    def _fetch(self) -> Optional[Dict[str, Any]]:
        if self._cache_is_fresh():
            cached = self._load_cache()
            if cached:
                return cached

        url = f"{_API_BASE}/{self._scheme_code}"
        try:
            resp = requests.get(url, timeout=15)
            resp.raise_for_status()
            payload = resp.json()
            self._save_cache(payload)
            return payload
        except Exception as exc:
            logger.warning("Liquid fund API request failed: %s", exc)
            return self._load_cache()

    def _parse_nav_entries(
        self, data: List[Dict[str, str]]
    ) -> List[LiquidFundNAV]:
        """Parse raw NAV entries into dataclass objects with daily returns."""
        entries: List[LiquidFundNAV] = []
        scheme_name = ""
        for item in reversed(data):
            try:
                nav_val = float(item["nav"])
                nav_date = datetime.strptime(item["date"], "%d-%m-%Y").date()
            except (KeyError, ValueError):
                continue
            daily_ret = 0.0
            if entries:
                prev_nav = entries[-1].nav
                if prev_nav > 0:
                    daily_ret = ((nav_val / prev_nav) - 1.0) * 100.0
            entries.append(
                LiquidFundNAV(
                    scheme_code=self._scheme_code,
                    scheme_name=scheme_name,
                    nav=nav_val,
                    date=nav_date,
                    daily_return_pct=daily_ret,
                )
            )
        return entries

    def current_nav(self) -> Optional[LiquidFundNAV]:
        """Fetch the latest NAV and compute daily return."""
        payload = self._fetch()
        if not payload:
            return None

        scheme_name = (payload.get("meta") or {}).get("scheme_name", "")
        nav_data = payload.get("data", [])
        if len(nav_data) < 2:
            return None

        try:
            latest = nav_data[0]
            previous = nav_data[1]
            latest_nav = float(latest["nav"])
            prev_nav = float(previous["nav"])
            nav_date = datetime.strptime(latest["date"], "%d-%m-%Y").date()
        except (KeyError, ValueError, IndexError) as exc:
            logger.warning("Failed to parse liquid fund NAV: %s", exc)
            return None

        daily_ret = ((latest_nav / prev_nav) - 1.0) * 100.0 if prev_nav > 0 else 0.0

        return LiquidFundNAV(
            scheme_code=self._scheme_code,
            scheme_name=scheme_name,
            nav=latest_nav,
            date=nav_date,
            daily_return_pct=daily_ret,
        )

    def annualised_return(self, days: int = 7) -> Optional[float]:
        """Trailing annualised return over the given window."""
        payload = self._fetch()
        if not payload:
            return None

        nav_data = payload.get("data", [])
        if len(nav_data) < days + 1:
            return None

        try:
            latest_nav = float(nav_data[0]["nav"])
            older_nav = float(nav_data[days]["nav"])
        except (KeyError, ValueError, IndexError):
            return None

        if older_nav <= 0:
            return None

        period_return = latest_nav / older_nav - 1.0
        annualised = ((1.0 + period_return) ** (365.0 / days) - 1.0) * 100.0
        return annualised
