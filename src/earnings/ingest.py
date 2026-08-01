"""Poll Screener.in for quarterly results with on-disk cache."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd

from src.backtest.screener_quarters import (
    QUARTERS_COLUMNS,
    ScreenerQuarterlyClient,
    parse_announcements_html,
    parse_quarters_html,
)
from src.config import Config, get_config
from src.logging_setup import get_logger

logger = get_logger(__name__)


@dataclass
class PollResult:
    symbol: str
    quarters: pd.DataFrame
    fetched_at: datetime
    latest_announcement: Optional[date] = None
    from_cache: bool = False


class EarningsIngestor:
    """Fetches quarterly tables for symbols the fund cares about."""

    def __init__(self, config: Optional[Config] = None):
        self._config = config or get_config()
        self._client = ScreenerQuarterlyClient(
            request_delay=float(
                self._config.get("fundamentals.request_delay_seconds", 1.0) or 1.0
            )
        )

    @property
    def _cache_dir(self) -> Path:
        return self._config.cache_dir / "earnings"

    def _cache_path(self, symbol: str) -> Path:
        return self._cache_dir / f"{symbol.upper()}.json"

    def _cache_ttl_seconds(self) -> float:
        hours = float(self._config.get("earnings.cache_ttl_hours", 24) or 24)
        return hours * 3600.0

    def _load_cache(self, symbol: str) -> Optional[PollResult]:
        path = self._cache_path(symbol)
        if not path.exists():
            return None
        if (time.time() - path.stat().st_mtime) > self._cache_ttl_seconds():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            quarters = pd.DataFrame(payload.get("quarters") or [])
            if quarters.empty:
                return None
            for column in QUARTERS_COLUMNS:
                if column not in quarters.columns:
                    quarters[column] = None
            announcement = payload.get("latest_announcement")
            return PollResult(
                symbol=symbol.upper(),
                quarters=quarters[QUARTERS_COLUMNS],
                fetched_at=datetime.fromisoformat(payload["fetched_at"]),
                latest_announcement=date.fromisoformat(announcement)
                if announcement
                else None,
                from_cache=True,
            )
        except (json.JSONDecodeError, KeyError, ValueError, OSError) as exc:
            logger.debug("Earnings cache unreadable for %s: %s", symbol, exc)
            return None

    def _save_cache(
        self,
        symbol: str,
        quarters: pd.DataFrame,
        latest_announcement: Optional[date],
    ) -> None:
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "symbol": symbol.upper(),
            "fetched_at": datetime.utcnow().isoformat(),
            "latest_announcement": latest_announcement.isoformat()
            if latest_announcement
            else None,
            "quarters": quarters.to_dict(orient="records"),
        }
        self._cache_path(symbol).write_text(
            json.dumps(payload, default=str), encoding="utf-8"
        )

    def poll(self, symbol: str) -> Optional[PollResult]:
        symbol = symbol.upper()
        cached = self._load_cache(symbol)
        if cached is not None:
            return cached

        html, _flavour = self._client.fetch_page(symbol)
        if html is None:
            logger.debug("No Screener page for %s", symbol)
            return None

        quarters = parse_quarters_html(html)
        if quarters.empty:
            return None

        latest_announcement: Optional[date] = None
        announcements = parse_announcements_html(html)
        dated = [
            date.fromisoformat(str(entry["date"]))
            for entry in announcements
            if entry.get("results_related") and entry.get("date")
        ]
        if dated:
            latest_announcement = max(dated)

        self._save_cache(symbol, quarters, latest_announcement)
        return PollResult(
            symbol=symbol,
            quarters=quarters,
            fetched_at=datetime.utcnow(),
            latest_announcement=latest_announcement,
            from_cache=False,
        )

    def poll_many(self, symbols: List[str]) -> Dict[str, PollResult]:
        out: Dict[str, PollResult] = {}
        for symbol in dict.fromkeys(s.upper() for s in symbols):
            result = self.poll(symbol)
            if result is not None:
                out[symbol] = result
        return out

    def close(self) -> None:
        self._client.close()
