"""Approved, evidence-only source boundaries for curated research agents."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import requests

from src.workspace.agent_platform import AgentContract, SourceCategory, SourceRecord, WorkScope

EODHD_BASE_URL = "https://eodhd.com/api"
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
NSE_DISCLOSURES_URL = "https://www.nseindia.com/api/corporate-announcements?index=equities&symbol={symbol}"
REQUEST_TIMEOUT_SECONDS = 15


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _unavailable(label: str, category: SourceCategory, reason: str) -> SourceRecord:
    return SourceRecord(label=label, category=category, coverage_status="unavailable", details={"reason": reason})


class AgentSourceCollector:
    """Collect normalized source metadata without retaining raw vendor documents."""

    def __init__(self, session: Optional[requests.Session] = None) -> None:
        self._session = session or requests.Session()

    def collect(self, contract: AgentContract, scope: WorkScope, completed_artifacts: int = 0) -> List[SourceRecord]:
        scope = WorkScope.model_validate(scope)
        records: List[SourceRecord] = []
        for category in contract.required_sources:
            if category in {"market_data", "fundamentals", "news"}:
                records.append(self._eodhd(category, scope))
            elif category == "filings":
                records.append(self._filings(scope))
            elif category == "portfolio_snapshot":
                records.append(_unavailable("Portfolio snapshot", category, "Portfolio snapshot access is enabled with the Phase 3 worker."))
            elif category == "case_artifacts":
                records.append(self._case_artifacts(completed_artifacts))
        return records

    def _eodhd(self, category: SourceCategory, scope: WorkScope) -> SourceRecord:
        token = os.getenv("EODHD_API_TOKEN", "").strip()
        if not token:
            return _unavailable("EODHD", category, "EODHD_API_TOKEN is not configured.")
        if not scope.symbols:
            return _unavailable("EODHD", category, "A symbol is required for this source.")

        symbol = scope.symbols[0]
        endpoint = {
            "market_data": f"real-time/{symbol}",
            "fundamentals": f"fundamentals/{symbol}",
            "news": "news",
        }[category]
        params: Dict[str, str] = {"api_token": token, "fmt": "json"}
        if category == "news":
            params["s"] = symbol
        try:
            response = self._session.get(f"{EODHD_BASE_URL}/{endpoint}", params=params, timeout=REQUEST_TIMEOUT_SECONDS)
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            return _unavailable("EODHD", category, f"EODHD retrieval failed: {type(exc).__name__}.")
        if not isinstance(payload, (dict, list)) or not payload:
            return _unavailable("EODHD", category, "EODHD returned no usable records.")

        observed_at = self._observed_at(payload)
        status = "stale" if observed_at and self._is_stale(observed_at) else "available"
        return SourceRecord(
            label="EODHD",
            category=category,
            source_url="https://eodhd.com/financial-apis/",
            observed_at=observed_at or _now(),
            coverage_status=status,
            details={"symbol": symbol, "provider": "eodhd", "record_type": category},
        )

    def _filings(self, scope: WorkScope) -> SourceRecord:
        market = (scope.market or "").lower()
        exchange = (scope.exchange or "").upper()
        if "us" in market or exchange in {"NYSE", "NASDAQ"}:
            return self._sec_filing(scope)
        if "india" in market or exchange in {"NSE", "BSE"}:
            return self._exchange_disclosure(scope, exchange or "NSE")
        return _unavailable("Primary filings", "filings", "Market must identify US, NSE, or BSE coverage.")

    def _sec_filing(self, scope: WorkScope) -> SourceRecord:
        user_agent = os.getenv("SEC_USER_AGENT", "").strip()
        if not user_agent:
            return _unavailable("SEC EDGAR", "filings", "SEC_USER_AGENT is not configured.")
        if not scope.symbols:
            return _unavailable("SEC EDGAR", "filings", "A symbol is required for SEC filings.")
        try:
            headers = {"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"}
            tickers = self._session.get(SEC_TICKERS_URL, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
            tickers.raise_for_status()
            matches = [item for item in tickers.json().values() if str(item.get("ticker", "")).upper() == scope.symbols[0]]
            if not matches:
                return _unavailable("SEC EDGAR", "filings", "No SEC registrant matched the requested symbol.")
            cik = str(matches[0]["cik_str"]).zfill(10)
            facts = self._session.get(SEC_FACTS_URL.format(cik=cik), headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
            facts.raise_for_status()
            payload = facts.json()
        except (requests.RequestException, ValueError, KeyError) as exc:
            return _unavailable("SEC EDGAR", "filings", f"SEC filing retrieval failed: {type(exc).__name__}.")
        if not isinstance(payload, dict) or not payload:
            return _unavailable("SEC EDGAR", "filings", "SEC returned no usable filing facts.")
        return SourceRecord(label="SEC EDGAR", category="filings", source_url=f"https://data.sec.gov/submissions/CIK{cik}.json", observed_at=_now(), coverage_status="available", details={"symbol": scope.symbols[0], "cik": cik, "provider": "sec_edgar"})

    def _exchange_disclosure(self, scope: WorkScope, exchange: str) -> SourceRecord:
        if os.getenv("WORKSPACE_PRIMARY_DISCLOSURES_ENABLED", "false").lower() not in {"1", "true", "yes"}:
            return _unavailable(f"{exchange} disclosures", "filings", "Primary exchange disclosure retrieval is disabled.")
        if not scope.symbols:
            return _unavailable(f"{exchange} disclosures", "filings", "A symbol is required for exchange disclosures.")
        if exchange == "NSE":
            url = NSE_DISCLOSURES_URL.format(symbol=scope.symbols[0])
            headers = {"User-Agent": "Nivelo-Research/1.0"}
        else:
            url = os.getenv("BSE_DISCLOSURES_URL", "").strip()
            api_key = os.getenv("BSE_DATAFEED_API_KEY", "").strip()
            if not url or not api_key:
                return _unavailable("BSE disclosures", "filings", "BSE data-feed URL and API key are not configured.")
            headers = {"X-API-Key": api_key}
        try:
            response = self._session.get(url, headers=headers, params={"symbol": scope.symbols[0]} if exchange == "BSE" else None, timeout=REQUEST_TIMEOUT_SECONDS)
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            return _unavailable(f"{exchange} disclosures", "filings", f"Exchange disclosure retrieval failed: {type(exc).__name__}.")
        if not isinstance(payload, (dict, list)) or not payload:
            return _unavailable(f"{exchange} disclosures", "filings", "Exchange returned no usable disclosures.")
        return SourceRecord(label=f"{exchange} disclosures", category="filings", source_url=url, observed_at=_now(), coverage_status="available", details={"symbol": scope.symbols[0], "exchange": exchange})

    @staticmethod
    def _case_artifacts(completed_artifacts: int) -> SourceRecord:
        if not completed_artifacts:
            return _unavailable("Completed case artifacts", "case_artifacts", "No completed case artifacts are available.")
        return SourceRecord(label="Completed case artifacts", category="case_artifacts", observed_at=_now(), coverage_status="available", details={"artifact_count": completed_artifacts})

    @staticmethod
    def _observed_at(payload: Any) -> Optional[str]:
        item = payload[0] if isinstance(payload, list) and payload else payload
        if not isinstance(item, dict):
            return None
        value = item.get("timestamp") or item.get("updated_at") or item.get("date")
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, timezone.utc).isoformat()
        return str(value) if value else None

    @staticmethod
    def _is_stale(observed_at: str) -> bool:
        try:
            parsed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        except ValueError:
            return False
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed < datetime.now(timezone.utc) - timedelta(days=3)
