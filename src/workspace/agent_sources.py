"""Approved, evidence-only source tools for live research-agent runs.

Each method on :class:`AgentSourceCollector` maps one-to-one to an
``allowed_tool`` in the curated contracts. A tool returns a bounded,
JSON-serializable payload for the model and records a ``SourceRecord`` for the
audit trail. Unavailable or failed fetches return an explicit
``coverage_status: "unavailable"`` payload instead of raising, so a run never
invents evidence and always leaves an inspectable source trail.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import requests

from src.config import Config, get_config
from src.workspace.agent_platform import AgentWorkRepository, SourceCategory, SourceRecord, WorkScope

EODHD_BASE_URL = "https://eodhd.com/api"
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
NSE_DISCLOSURES_URL = "https://www.nseindia.com/api/corporate-announcements?index=equities&symbol={symbol}"
REQUEST_TIMEOUT_SECONDS = 15
MAX_NEWS_ITEMS = 8


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _unavailable(label: str, category: SourceCategory, reason: str) -> SourceRecord:
    return SourceRecord(label=label, category=category, coverage_status="unavailable", details={"reason": reason})


def _trim_text(text: Any, limit: int = 300) -> str:
    value = str(text or "").strip()
    return value if len(value) <= limit else value[:limit] + "…"


class AgentSourceCollector:
    """Bound source tools for one live agent run.

    Construct with the run context, then register its methods as the agent's
    tools. ``sources`` accumulates every tool outcome so the run can be
    validated and audited after the model finishes.
    """

    def __init__(
        self,
        config: Optional[Config] = None,
        scope: Optional[WorkScope] = None,
        repo: Optional[AgentWorkRepository] = None,
        run: Optional[Dict[str, Any]] = None,
        session: Optional[requests.Session] = None,
    ) -> None:
        self._config = config or get_config()
        self._session = session or requests.Session()
        self.scope = scope or WorkScope()
        self._repo = repo or AgentWorkRepository(self._config)
        self._run = run or {}
        self.sources: List[SourceRecord] = []

    def record(self, source: SourceRecord) -> None:
        for existing in self.sources:
            if (
                existing.label == source.label
                and existing.category == source.category
                and existing.coverage_status == source.coverage_status
                and existing.details == source.details
            ):
                return
        self.sources.append(source)

    def _resolve_symbol(self, symbol: Optional[str]) -> str:
        candidate = (symbol or "").strip().upper()
        if candidate:
            return candidate
        if self.scope.symbols:
            return self.scope.symbols[0]
        raise ValueError("A symbol is required for this source.")

    def _payload(self, source: SourceRecord, data: Any) -> Dict[str, Any]:
        return {"coverage_status": source.coverage_status, "provider": source.label, "data": data, "reason": source.details.get("reason")}

    # ------------------------------------------------------------------
    # EODHD market data / fundamentals / news
    # ------------------------------------------------------------------
    def fetch_eodhd_market_data(self, symbol: Optional[str] = None) -> Dict[str, Any]:
        token = os.getenv("EODHD_API_TOKEN", "").strip()
        if not token:
            return self._unavailable_result("EODHD", "market_data", "EODHD_API_TOKEN is not configured.")
        try:
            resolved = self._resolve_symbol(symbol)
        except ValueError as exc:
            return self._unavailable_result("EODHD", "market_data", str(exc))
        source, payload = self._eodhd("market_data", f"real-time/{resolved}", resolved)
        return self._payload(source, payload)

    def fetch_eodhd_fundamentals(self, symbol: Optional[str] = None) -> Dict[str, Any]:
        token = os.getenv("EODHD_API_TOKEN", "").strip()
        if not token:
            return self._unavailable_result("EODHD", "fundamentals", "EODHD_API_TOKEN is not configured.")
        try:
            resolved = self._resolve_symbol(symbol)
        except ValueError as exc:
            return self._unavailable_result("EODHD", "fundamentals", str(exc))
        source, payload = self._eodhd("fundamentals", f"fundamentals/{resolved}", resolved)
        return self._payload(source, payload)

    def fetch_eodhd_news(self, symbol: Optional[str] = None) -> Dict[str, Any]:
        token = os.getenv("EODHD_API_TOKEN", "").strip()
        if not token:
            return self._unavailable_result("EODHD", "news", "EODHD_API_TOKEN is not configured.")
        try:
            resolved = self._resolve_symbol(symbol)
        except ValueError as exc:
            return self._unavailable_result("EODHD", "news", str(exc))
        source, payload = self._eodhd("news", "news", resolved)
        return self._payload(source, payload)

    def _eodhd(self, category: SourceCategory, endpoint: str, symbol: str) -> tuple[SourceRecord, Any]:
        token = os.getenv("EODHD_API_TOKEN", "").strip()
        params: Dict[str, str] = {"api_token": token, "fmt": "json"}
        if category == "news":
            params["s"] = symbol
        try:
            response = self._session.get(f"{EODHD_BASE_URL}/{endpoint}", params=params, timeout=REQUEST_TIMEOUT_SECONDS)
            response.raise_for_status()
            raw = response.json()
        except (requests.RequestException, ValueError) as exc:
            return self._unavailable_source("EODHD", category, f"EODHD retrieval failed: {type(exc).__name__}."), None
        if not isinstance(raw, (dict, list)) or not raw:
            return self._unavailable_source("EODHD", category, "EODHD returned no usable records."), None
        payload = self._compact_eodhd(category, raw)
        observed_at = self._observed_at(raw)
        status = "stale" if observed_at and self._is_stale(observed_at) else "available"
        source = SourceRecord(
            label="EODHD",
            category=category,
            source_url="https://eodhd.com/financial-apis/",
            observed_at=observed_at or _now(),
            coverage_status=status,
            details={"symbol": symbol, "provider": "eodhd", "record_type": category},
        )
        self.record(source)
        return source, payload

    @staticmethod
    def _compact_eodhd(category: SourceCategory, raw: Any) -> Dict[str, Any]:
        if category == "market_data":
            return AgentSourceCollector._pick(raw, ["code", "timestamp", "open", "high", "low", "close", "volume", "previousClose", "change_pct", "last_updated"])
        if category == "news":
            items = raw if isinstance(raw, list) else raw.get("news", [])
            return {"items": [
                {"title": _trim_text(item.get("title"), 160), "date": item.get("date"), "link": item.get("link")}
                for item in (items[:MAX_NEWS_ITEMS] if isinstance(items, list) else [])
            ]}
        # fundamentals: keep bounded, decision-relevant slices.
        sections = {
            "Highlights": raw.get("Highlights"),
            "Valuation": raw.get("Valuation"),
            "Earnings": raw.get("Earnings"),
            "Growth": raw.get("Growth"),
            "Company": AgentSourceCollector._pick(raw.get("General", {}), ["CompanyName", "GICS_Sector", "GICS_Industry", "MarketCapitalization", "CurrencyCode"]),
        }
        return {key: value for key, value in sections.items() if isinstance(value, (dict, list)) and value}

    # ------------------------------------------------------------------
    # Primary filings (SEC EDGAR for US; NSE/BSE gated)
    # ------------------------------------------------------------------
    def fetch_primary_filings(self, symbol: Optional[str] = None, market: Optional[str] = None) -> Dict[str, Any]:
        try:
            resolved = self._resolve_symbol(symbol)
        except ValueError as exc:
            return self._unavailable_result("Primary filings", "filings", str(exc))
        scope_market = (market or self.scope.market or "").lower()
        exchange = (self.scope.exchange or "").upper()
        if "us" in scope_market or exchange in {"NYSE", "NASDAQ"}:
            source, payload = self._sec_filing(resolved)
        elif "india" in scope_market or exchange in {"NSE", "BSE"}:
            source, payload = self._exchange_disclosure(resolved, exchange or "NSE")
        else:
            return self._unavailable_result("Primary filings", "filings", "Market must identify US, NSE, or BSE coverage.")
        return self._payload(source, payload)

    def _sec_filing(self, symbol: str) -> tuple[SourceRecord, Any]:
        user_agent = os.getenv("SEC_USER_AGENT", "").strip()
        if not user_agent:
            return self._unavailable_source("SEC EDGAR", "filings", "SEC_USER_AGENT is not configured."), None
        try:
            headers = {"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"}
            tickers = self._session.get(SEC_TICKERS_URL, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
            tickers.raise_for_status()
            matches = [item for item in tickers.json().values() if str(item.get("ticker", "")).upper() == symbol]
            if not matches:
                return self._unavailable_source("SEC EDGAR", "filings", "No SEC registrant matched the requested symbol."), None
            cik = str(matches[0]["cik_str"]).zfill(10)
            facts = self._session.get(SEC_FACTS_URL.format(cik=cik), headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
            facts.raise_for_status()
            raw = facts.json()
        except (requests.RequestException, ValueError, KeyError) as exc:
            return self._unavailable_source("SEC EDGAR", "filings", f"SEC filing retrieval failed: {type(exc).__name__}."), None
        if not isinstance(raw, dict) or not raw:
            return self._unavailable_source("SEC EDGAR", "filings", "SEC returned no usable filing facts."), None
        source = SourceRecord(
            label="SEC EDGAR",
            category="filings",
            source_url=f"https://data.sec.gov/submissions/CIK{cik}.json",
            observed_at=_now(),
            coverage_status="available",
            details={"symbol": symbol, "cik": cik, "provider": "sec_edgar"},
        )
        self.record(source)
        return source, self._compact_sec(raw)

    @staticmethod
    def _compact_sec(raw: Dict[str, Any]) -> Dict[str, Any]:
        """Latest annual XBRL facts that matter, not the raw companyfacts dump."""
        wanted = {
            "Revenues": "us-gaap.RevenueFromContractWithCustomerExcludingAssessedTax",
            "Revenue": "us-gaap.Revenues",
            "NetIncomeLoss": "us-gaap.NetIncomeLoss",
            "Assets": "us-gaap.Assets",
            "StockholdersEquity": "us-gaap.StockholdersEquity",
        }
        facts = raw.get("facts", {})
        gaap = facts.get("us-gaap", {}) if isinstance(facts, dict) else {}
        out: Dict[str, Any] = {}
        for label, key in wanted.items():
            block = gaap.get(key)
            if not isinstance(block, dict):
                continue
            units = block.get("units", {})
            rows = units.get("USD") or units.get("usd") or []
            annual = [row for row in rows if row.get("form") in {"10-K", "10-K/A"} and row.get("fp") == "FY"]
            row = (annual or rows or [None])[-1] if rows else None
            if row:
                out[label] = {"value": row.get("val"), "end": row.get("end"), "filed": row.get("filed"), "frame": row.get("frame")}
        entity = raw.get("entityName") or raw.get("entity", {}).get("cik")
        return {"entityName": entity, "facts_usd": out}

    def _exchange_disclosure(self, symbol: str, exchange: str) -> tuple[SourceRecord, Any]:
        if os.getenv("WORKSPACE_PRIMARY_DISCLOSURES_ENABLED", "false").lower() not in {"1", "true", "yes"}:
            return self._unavailable_source(f"{exchange} disclosures", "filings", "Primary exchange disclosure retrieval is disabled."), None
        if exchange == "NSE":
            url = NSE_DISCLOSURES_URL.format(symbol=symbol)
            headers = {"User-Agent": "Nivelo-Research/1.0"}
        else:
            url = os.getenv("BSE_DISCLOSURES_URL", "").strip()
            api_key = os.getenv("BSE_DATAFEED_API_KEY", "").strip()
            if not url or not api_key:
                return self._unavailable_source("BSE disclosures", "filings", "BSE data-feed URL and API key are not configured."), None
            headers = {"X-API-Key": api_key}
        try:
            response = self._session.get(url, headers=headers, params={"symbol": symbol} if exchange == "BSE" else None, timeout=REQUEST_TIMEOUT_SECONDS)
            response.raise_for_status()
            raw = response.json()
        except (requests.RequestException, ValueError) as exc:
            return self._unavailable_source(f"{exchange} disclosures", "filings", f"Exchange disclosure retrieval failed: {type(exc).__name__}."), None
        if not isinstance(raw, (dict, list)) or not raw:
            return self._unavailable_source(f"{exchange} disclosures", "filings", "Exchange returned no usable disclosures."), None
        source = SourceRecord(label=f"{exchange} disclosures", category="filings", source_url=url, observed_at=_now(), coverage_status="available", details={"symbol": symbol, "exchange": exchange})
        self.record(source)
        return source, {"disclosures": raw if isinstance(raw, list) else raw.get("data", raw)}

    # ------------------------------------------------------------------
    # Portfolio snapshot (frozen EOD persona) and completed case artifacts
    # ------------------------------------------------------------------
    def read_portfolio_snapshot(self, persona_id: Optional[str] = None) -> Dict[str, Any]:
        from src.workspace.dashboard_snapshot import DashboardSnapshotService

        pid = (persona_id or self.scope.portfolio_persona_id or "").strip()
        if not pid:
            return self._unavailable_result("Portfolio snapshot", "portfolio_snapshot", "A portfolio persona id is required.")
        snapshot = DashboardSnapshotService(self._config).latest()
        persona = None
        if snapshot:
            persona = next((row for row in snapshot.get("personas", []) if row.get("id") == pid), None)
        if persona is None:
            return self._unavailable_result("Portfolio snapshot", "portfolio_snapshot", "No published snapshot exists for this persona.")
        overview = persona.get("overview") or {}
        meta = persona.get("metadata") or {}
        risk = persona.get("risk") or {}
        holdings = sorted(persona.get("holdings") or [], key=lambda h: -(h.get("weight_pct") or 0))[:12]
        source = SourceRecord(
            label="Portfolio snapshot",
            category="portfolio_snapshot",
            source_url=None,
            observed_at=(snapshot or {}).get("as_of") or _now(),
            coverage_status="available",
            details={"persona_id": pid, "fund_name": meta.get("fund_name") or pid, "position_count": overview.get("position_count")},
        )
        self.record(source)
        payload = {
            "fund_name": meta.get("fund_name") or pid,
            "as_of": (snapshot or {}).get("snapshot_date"),
            "overview": AgentSourceCollector._pick(overview, ["total_value", "total_return_pct", "position_count", "cash_pct"]),
            "risk": AgentSourceCollector._pick(risk, ["portfolio_beta", "market_regime", "warnings", "style_tilts", "concentration_top5_pct"]),
            "top_holdings": [AgentSourceCollector._pick(h, ["symbol", "company_name", "sector", "weight_pct", "return_pct"]) for h in holdings],
        }
        return self._payload(source, payload)

    def read_case_artifacts(self) -> Dict[str, Any]:
        run_id = self._run.get("id")
        case_id = self._run.get("case_id")
        if not case_id:
            return self._unavailable_result("Completed case artifacts", "case_artifacts", "No case is attached to this run.")
        case = self._repo.case(case_id)
        artifacts = [
            {"run_id": item["id"], "agent_type": item["agent_type"], "status": item["status"], "artifact": artifact}
            for item in case["runs"]
            if item["id"] != run_id and item["status"] == "completed"
            for artifact in item["artifacts"]
        ]
        if not artifacts:
            return self._unavailable_result("Completed case artifacts", "case_artifacts", "No completed case artifacts are available.")
        run_ids = sorted({item["run_id"] for item in artifacts})
        source = SourceRecord(
            label="Completed case artifacts",
            category="case_artifacts",
            observed_at=_now(),
            coverage_status="available",
            details={"artifact_count": len(artifacts), "artifact_run_ids": run_ids},
        )
        self.record(source)
        return self._payload(source, {"artifacts": artifacts, "run_ids": run_ids})

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _unavailable_source(self, label: str, category: SourceCategory, reason: str) -> SourceRecord:
        source = _unavailable(label, category, reason)
        self.record(source)
        return source

    def _unavailable_result(self, label: str, category: SourceCategory, reason: str) -> Dict[str, Any]:
        source = self._unavailable_source(label, category, reason)
        return self._payload(source, None)

    @staticmethod
    def _pick(value: Any, keys: List[str]) -> Dict[str, Any]:
        return {key: value.get(key) for key in keys if isinstance(value, dict) and value.get(key) is not None}

    @staticmethod
    def _observed_at(payload: Any) -> Optional[str]:
        item = payload[0] if isinstance(payload, list) and payload else payload
        if not isinstance(item, dict):
            return None
        value = item.get("timestamp") or item.get("last_updated") or item.get("date")
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


TOOL_SPECS: Dict[str, Dict[str, Any]] = {
    "fetch_eodhd_market_data": {
        "description": "Fetch live/trailing market quote data for one symbol from EODHD (price, volume, change).",
        "parameters": {"type": "object", "properties": {"symbol": {"type": "string", "description": "Ticker symbol, e.g. NVDA or TCS.NS. Defaults to the case scope symbol."}}},
    },
    "fetch_eodhd_fundamentals": {
        "description": "Fetch company fundamentals for one symbol from EODHD (highlights, valuation, earnings, growth).",
        "parameters": {"type": "object", "properties": {"symbol": {"type": "string", "description": "Ticker symbol, e.g. NVDA or TCS.NS. Defaults to the case scope symbol."}}},
    },
    "fetch_eodhd_news": {
        "description": "Fetch recent news headlines for one symbol from EODHD.",
        "parameters": {"type": "object", "properties": {"symbol": {"type": "string", "description": "Ticker symbol, e.g. NVDA or TCS.NS. Defaults to the case scope symbol."}}},
    },
    "fetch_primary_filings": {
        "description": "Fetch primary filing facts (SEC EDGAR for US, exchange disclosures for NSE/BSE) for one symbol.",
        "parameters": {"type": "object", "properties": {"symbol": {"type": "string", "description": "Ticker symbol. Defaults to the case scope symbol."}, "market": {"type": "string", "description": "Market, e.g. 'US equities' or 'India equities'. Defaults to the case scope market."}}},
    },
    "read_portfolio_snapshot": {
        "description": "Read the frozen published snapshot (holdings, risk, overview) for a paper persona.",
        "parameters": {"type": "object", "properties": {"persona_id": {"type": "string", "description": "Persona id, e.g. 'balanced'. Defaults to the case scope persona."}}},
    },
    "read_case_artifacts": {
        "description": "Read the completed research artifacts already recorded in this case.",
        "parameters": {"type": "object", "properties": {}},
    },
}


def tool_schema(tool_name: str) -> Optional[Dict[str, Any]]:
    spec = TOOL_SPECS.get(tool_name)
    if not spec:
        return None
    return {"type": "function", "function": {"name": tool_name, "description": spec["description"], "parameters": spec["parameters"]}}


def invoke_tool(collector: "AgentSourceCollector", tool_name: str, arguments: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    method = getattr(collector, tool_name, None)
    if method is None:
        return {"coverage_status": "unavailable", "provider": tool_name, "data": None, "reason": "Unknown tool."}
    try:
        return method(**{key: value for key, value in (arguments or {}).items() if value is not None})
    except Exception as exc:  # a tool must never crash the run
        return {"coverage_status": "unavailable", "provider": tool_name, "data": None, "reason": f"{type(exc).__name__}: {exc}"}
