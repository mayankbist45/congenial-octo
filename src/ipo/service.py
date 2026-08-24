"""Source-first IPO discovery and research.

The service deliberately treats a prospectus as evidence, not as a trading
signal. It exposes the source URL and coverage gaps with every dossier so an
analyst can verify the conclusion before making an investment decision.
"""

from __future__ import annotations

import io
import math
import re
import zipfile
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from src.config import Config, get_config
from src.llm import LLMClient, LLMUnavailable
from src.logging_setup import get_logger
from src.journal.db import connect

logger = get_logger(__name__)

SEBI_PUBLIC_ISSUES_URL = (
    "https://www.sebi.gov.in/sebiweb/home/HomeAction.do?doListing=yes&sid=3&smid=10&ssid=15"
)
NSE_CURRENT_ISSUES_URL = "https://www.nseindia.com/api/ipo-current-issue"
NSE_ISSUE_DETAIL_URL = "https://www.nseindia.com/api/ipo-detail?symbol={symbol}"
NSE_IPO_PAGE_URL = "https://www.nseindia.com/market-data/all-upcoming-issues-ipo"
BSE_ISSUE_SUMMARY_URL = "https://www.bseindia.com/markets/PublicIssues/Issuesummary.aspx"
DOCUMENT_TIMEOUT_SECONDS = 25
MAX_DOCUMENT_CHARS = 90_000
IST = ZoneInfo("Asia/Kolkata")
GMP_SOURCES = ("investorgain", "zoom_news")


class IpoResearchError(RuntimeError):
    """Raised when a public issue document cannot be retrieved or read."""


class IpoResearchService:
    """Discover SEBI public issues and turn an offer document into a dossier."""

    def __init__(
        self, config: Optional[Config] = None, session: Optional[requests.Session] = None
    ) -> None:
        self._config = config or get_config()
        self._session = session or requests.Session()
        self._llm = LLMClient(self._config)
        self._nse_details: Dict[str, Dict[str, Any]] = {}

    def list_issues(self, limit: int = 24) -> List[Dict[str, str]]:
        """Return the newest DRHP/RHP issues published on the SEBI listing."""
        response = self._session.get(
            SEBI_PUBLIC_ISSUES_URL,
            timeout=DOCUMENT_TIMEOUT_SECONDS,
            headers={"User-Agent": "Nivelo-IPO-Research/1.0"},
        )
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        issues: List[Dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for anchor in soup.select("a[href]"):
            title = " ".join(anchor.get_text(" ", strip=True).split())
            if not title or not re.search(r"\b(?:U?DRHP|RHP|prospectus)\b", title, re.I):
                continue
            href = urljoin(SEBI_PUBLIC_ISSUES_URL, anchor["href"])
            kind_match = re.search(r"\b(U?DRHP|RHP|prospectus)\b", title, re.I)
            kind = kind_match.group(1).upper() if kind_match else "OFFER_DOCUMENT"
            issuer = re.sub(r"\s*[-–]?\s*(?:U?DRHP|RHP|prospectus).*", "", title, flags=re.I)
            key = (issuer.lower(), href)
            if key in seen:
                continue
            seen.add(key)
            issues.append(
                {"issuer": issuer.title(), "document_type": kind, "source_url": href}
            )
            if len(issues) >= limit:
                break
        return issues

    def research(
        self, issuer: str, source_url: str, document_text: Optional[str] = None
    ) -> Dict[str, Any]:
        """Create a review-ready dossier from an official offer document."""
        text = document_text or self._download_document(source_url)
        excerpt = self._trim_text(text)
        rules = self._rules_dossier(issuer, source_url, excerpt)
        dossier = rules
        if self._llm.available:
            try:
                dossier = self._llm_dossier(issuer, source_url, excerpt)
            except LLMUnavailable as exc:
                logger.warning("IPO LLM analysis unavailable for %s: %s", issuer, exc)
        # Coverage is measured from parsed official text, never from an LLM claim.
        dossier["coverage"] = rules["coverage"]
        dossier["data_gaps"] = rules["data_gaps"]
        return self._apply_publication_gate(dossier)

    def daily_analysis(self, limit: int = 6) -> Dict[str, Any]:
        """Build the compact IPO read model published by the daily workspace job."""
        try:
            discovered = self.list_issues(limit)
        except Exception as exc:
            logger.warning("IPO discovery unavailable: %s", exc)
            return {
                "issues": [],
                "warnings": ["Official IPO issue discovery was unavailable for this publication."],
                "data_status": "unavailable",
            }

        market_issues = self._current_nse_issues()
        market_by_name = {self._name_key(item.get("companyName", "")): item for item in market_issues}
        issues: List[Dict[str, Any]] = []
        warnings: List[str] = []
        active_documents = []
        for market in market_issues:
            details = self._nse_issue_details(str(market.get("symbol") or ""))
            market["_nse_details"] = details
            values = details.get("values") or {}
            if details and not (values.get("issue size") and (values.get("bid lot") or values.get("market lot"))):
                market["bse_issue_url"] = self._bse_issue_url(str(market.get("companyName") or ""))
            source_url = details.get("rhp_url") or self._nse_offer_document(str(market.get("symbol") or ""))
            if source_url:
                active_documents.append({
                    "issuer": str(market.get("companyName") or market.get("symbol")),
                    "document_type": "RHP",
                    "source_url": source_url,
                })
        active_keys = {self._name_key(issue["issuer"]) for issue in active_documents}
        candidates = active_documents + [issue for issue in discovered if self._name_key(issue["issuer"]) not in active_keys]
        for index, issue in enumerate(candidates, start=1):
            try:
                dossier = self.research(issue["issuer"], issue["source_url"])
            except (IpoResearchError, requests.RequestException) as exc:
                logger.warning("IPO research unavailable for %s: %s", issue["issuer"], exc)
                warnings.append(f"Offer-document analysis was unavailable for {issue['issuer']}.")
                slug = re.sub(r"[^a-z0-9]+", "-", issue["issuer"].lower()).strip("-")
                market = market_by_name.pop(self._name_key(issue["issuer"]), None)
                issues.append({
                    "id": str((market or {}).get("symbol") or f"{slug}-{index}").lower(),
                    "issuer": issue["issuer"], "document_type": issue["document_type"], "source_url": issue["source_url"],
                    "generated_at": datetime.now(timezone.utc).isoformat(), "status": "running" if market else "unconfirmed",
                    "issue_open": (market or {}).get("issueStartDate"), "issue_close": (market or {}).get("issueEndDate"),
                    "price_band": self._issue_terms(market).get("price_band", {}).get("value"), "issue_terms": self._issue_terms(market), "official_fields": {},
                    "dossier": self._unavailable_dossier("Official offer document could not be read: " + str(exc)),
                    "subscription": self._subscription_payload(market),
                    "pipeline_stages": [
                        {"name": "issues_discovery", "status": "completed", "summary": "Official issue discovered."},
                        {"name": "document_fetch", "status": "unavailable", "summary": "Official offer document could not be read."},
                        {"name": "fundamentals", "status": "unavailable", "summary": "Official-document coverage was insufficient."},
                        {"name": "risk_analytics", "status": "unavailable", "summary": "Official-document coverage was insufficient."},
                        {"name": "decision", "status": "unavailable", "summary": "No research verdict was published."},
                        {"name": "subscription_tracking", "status": "unavailable", "summary": "Awaiting a dated NSE or BSE bid-detail source."},
                    ],
                })
                continue
            slug = re.sub(r"[^a-z0-9]+", "-", issue["issuer"].lower()).strip("-")
            coverage = dossier.get("coverage") or {}
            market = market_by_name.pop(self._name_key(issue["issuer"]), None)
            terms = self._issue_terms(market)
            unavailable = dossier.get("research_status") == "unavailable"
            issues.append({
                "id": str((market or {}).get("symbol") or f"{slug}-{index}").lower(),
                "issuer": issue["issuer"],
                "document_type": issue["document_type"],
                "source_url": issue["source_url"],
                "generated_at": dossier.get("generated_at"),
                "status": "running" if market else "unconfirmed",
                "issue_open": (market or {}).get("issueStartDate"),
                "issue_close": (market or {}).get("issueEndDate"),
                "price_band": terms.get("price_band", {}).get("value"),
                "issue_terms": terms,
                "official_fields": self._official_fields(dossier, issue["source_url"]),
                "dossier": dossier,
                "subscription": self._subscription_payload(market),
                "pipeline_stages": [
                    {"name": "issues_discovery", "status": "completed", "summary": "Discovered from SEBI public-issue documents."},
                    {"name": "document_fetch", "status": "unavailable" if unavailable else "completed", "summary": dossier.get("research_block_reason") or "Official offer document analysed."},
                    {"name": "fundamentals", "status": "unavailable" if unavailable else ("completed" if coverage.get("financials") else "warning"), "summary": "Prospectus financial coverage assessed."},
                    {"name": "risk_analytics", "status": "unavailable" if unavailable else ("completed" if coverage.get("risks") else "warning"), "summary": "Offer-document risk factors assessed."},
                    {"name": "decision", "status": "unavailable" if unavailable else "completed", "summary": dossier.get("research_block_reason") or "Research verdict recorded for analyst review."},
                    {"name": "subscription_tracking", "status": "unavailable", "summary": "Awaiting a dated NSE or BSE bid-detail source."},
                ],
            })
        for market in market_by_name.values():
            issuer = str(market.get("companyName") or market.get("symbol") or "IPO")
            symbol = str(market.get("symbol") or self._name_key(issuer) or "ipo").lower()
            issues.append({
                "id": symbol,
                "issuer": issuer,
                "document_type": "NSE current issue",
                "source_url": NSE_IPO_PAGE_URL,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "status": "running",
                "issue_open": market.get("issueStartDate"),
                "issue_close": market.get("issueEndDate"),
                "price_band": market.get("issuePrice"),
                "issue_terms": self._issue_terms(market),
                "official_fields": {},
                "dossier": self._unavailable_dossier("Official offer document is not linked or readable for this issue."),
                "subscription": self._subscription_payload(market),
                "pipeline_stages": [
                    {"name": "issues_discovery", "status": "completed", "summary": "Current issue discovered from NSE."},
                    {"name": "document_fetch", "status": "unavailable", "summary": "Official offer document is not linked to this publication."},
                    {"name": "subscription_tracking", "status": "completed", "summary": "NSE total subscription multiple recorded."},
                ],
            })
        for issue in issues:
            issue["gmp_range"] = self._gmp_range(str(issue["id"]))
        return {"issues": issues, "warnings": warnings, "data_status": "available"}

    def _current_nse_issues(self) -> List[Dict[str, Any]]:
        """Read public NSE current-issue data after establishing its session cookie."""
        headers = {"User-Agent": "Mozilla/5.0", "Referer": NSE_IPO_PAGE_URL}
        try:
            self._session.get("https://www.nseindia.com/", timeout=DOCUMENT_TIMEOUT_SECONDS, headers=headers)
            response = self._session.get(NSE_CURRENT_ISSUES_URL, timeout=DOCUMENT_TIMEOUT_SECONDS, headers=headers)
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            logger.warning("NSE current IPO issue data unavailable: %s", exc)
            return []
        return [item for item in payload if isinstance(item, dict) and str(item.get("status", "")).lower() == "active"] if isinstance(payload, list) else []

    @staticmethod
    def _name_key(value: str) -> str:
        return re.sub(r"[^a-z0-9]", "", value.lower().replace("limited", ""))

    @staticmethod
    def _subscription_payload(issue: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        if not issue or issue.get("noOfTime") in (None, ""):
            return {"status": "unavailable", "message": "No dated exchange bid-detail source was available in this publication."}
        return {
            "status": "available",
            "total": issue["noOfTime"],
            "as_of": datetime.now(timezone.utc).isoformat(),
            "source_url": NSE_IPO_PAGE_URL,
            "source_type": "NSE",
        }

    def _download_document(self, source_url: str) -> str:
        response = self._session.get(
            source_url,
            timeout=DOCUMENT_TIMEOUT_SECONDS,
            headers={"User-Agent": "Nivelo-IPO-Research/1.0"},
        )
        response.raise_for_status()
        content_type = response.headers.get("content-type", "").lower()
        if "zip" in content_type or source_url.lower().split("?")[0].endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                pdf_name = next((name for name in archive.namelist() if name.lower().endswith(".pdf")), None)
                if not pdf_name:
                    raise IpoResearchError("The official offer-document archive did not contain a PDF.")
                return self._pdf_text(archive.read(pdf_name))
        if "pdf" in content_type or source_url.lower().split("?")[0].endswith(".pdf"):
            return self._pdf_text(response.content)
        return BeautifulSoup(response.text, "html.parser").get_text("\n", strip=True)

    @staticmethod
    def _pdf_text(content: bytes) -> str:
        try:
            from pypdf import PdfReader
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise IpoResearchError("PDF support is unavailable; install pypdf.") from exc
        reader = PdfReader(io.BytesIO(content))
        return "\n".join(page.extract_text() or "" for page in reader.pages)

    def _nse_offer_document(self, symbol: str) -> Optional[str]:
        return self._nse_issue_details(symbol).get("rhp_url")

    def _nse_issue_details(self, symbol: str) -> Dict[str, Any]:
        if not symbol:
            return {}
        if symbol in self._nse_details:
            return self._nse_details[symbol]
        headers = {"User-Agent": "Mozilla/5.0", "Referer": NSE_IPO_PAGE_URL}
        try:
            response = self._session.get(NSE_ISSUE_DETAIL_URL.format(symbol=symbol), timeout=DOCUMENT_TIMEOUT_SECONDS, headers=headers)
            response.raise_for_status()
            details = response.json()
        except (requests.RequestException, ValueError) as exc:
            logger.warning("NSE IPO detail unavailable for %s: %s", symbol, exc)
            return {}
        rows = ((details.get("issueInfo") or {}).get("dataList") or []) if isinstance(details, dict) else []
        values = {str(row.get("title") or "").strip().lower(): str(row.get("value") or "").strip() for row in rows if isinstance(row, dict)}
        result: Dict[str, Any] = {"source_url": NSE_IPO_PAGE_URL, "values": values}
        for row in rows:
            if str(row.get("title") or "").lower() == "red herring prospectus":
                value = str(row.get("value") or "").strip()
                if value.startswith("https://"):
                    result["rhp_url"] = value
        self._nse_details[symbol] = result
        return result

    def _issue_terms(self, market: Optional[Dict[str, Any]]) -> Dict[str, Dict[str, str]]:
        if not market:
            return {}
        details = market.get("_nse_details") or {}
        values = details.get("values") or {}
        source_url = details.get("source_url") or NSE_IPO_PAGE_URL
        bse = self._bse_issue_terms(str(market.get("bse_issue_url") or ""))
        def field(value: Any, fallback: str) -> Dict[str, str]:
            selected = str(value or bse.get(fallback, "")).strip()
            return {"value": selected, "source_url": source_url if value else bse.get("source_url", "")} if selected else {}
        return {
            "price_band": field(market.get("issuePrice") or values.get("price range") or values.get("price band"), "price_band"),
            "issue_size": field(values.get("issue size") or values.get("issue size - no. of shares"), "issue_size"),
            "lot_size": field(values.get("bid lot") or values.get("market lot") or values.get("minimum bid quantity"), "lot_size"),
        }

    def _bse_issue_terms(self, source_url: str) -> Dict[str, str]:
        """Use a supplied official BSE issue page only when NSE omits a term."""
        if not source_url.startswith("https://www.bseindia.com/"):
            return {}
        try:
            response = self._session.get(source_url, timeout=DOCUMENT_TIMEOUT_SECONDS, headers={"User-Agent": "Mozilla/5.0"})
            response.raise_for_status()
        except requests.RequestException:
            return {}
        text = BeautifulSoup(response.text, "html.parser").get_text(" ", strip=True)
        def match(label: str) -> str:
            found = re.search(label + r"\s*[:–-]?\s*([^|]{1,100})", text, re.I)
            return found.group(1).strip() if found else ""
        return {"source_url": source_url, "price_band": match("Price Band"), "issue_size": match("Issue Size"), "lot_size": match("(?:Market Lot|Minimum Bid Quantity)")}

    def _bse_issue_url(self, issuer: str) -> str:
        """Find the official BSE issue page used only as an NSE field fallback."""
        if not issuer:
            return ""
        try:
            response = self._session.get(BSE_ISSUE_SUMMARY_URL, timeout=DOCUMENT_TIMEOUT_SECONDS, headers={"User-Agent": "Mozilla/5.0"})
            response.raise_for_status()
        except requests.RequestException:
            return ""
        issuer_key = self._name_key(issuer)
        for anchor in BeautifulSoup(response.text, "html.parser").select("a[href]"):
            if issuer_key in self._name_key(anchor.get_text(" ", strip=True)):
                href = urljoin(BSE_ISSUE_SUMMARY_URL, str(anchor["href"]))
                if href.startswith("https://www.bseindia.com/"):
                    return href
        return ""

    @staticmethod
    def _official_fields(dossier: Dict[str, Any], source_url: str) -> Dict[str, Dict[str, str]]:
        return {
            name: {"value": str(dossier[name]), "source_url": source_url}
            for name in ("business", "financials", "use_of_proceeds")
            if dossier.get("coverage", {}).get(name) and dossier.get(name)
        }

    def _gmp_range(self, ipo_id: str) -> Optional[Dict[str, Any]]:
        now = datetime.now(timezone.utc)
        conn = connect(self._config)
        try:
            rows = conn.execute(
                "SELECT source_name, gmp_value, source_url, observed_at FROM ipo_gmp_observations WHERE ipo_id = ? AND source_name IN (?, ?) ORDER BY observed_at DESC",
                (ipo_id, *GMP_SOURCES),
            ).fetchall()
        finally:
            conn.close()
        latest: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            item = dict(row)
            latest.setdefault(item["source_name"], item)
        if set(latest) != set(GMP_SOURCES):
            return None
        observations = list(latest.values())
        try:
            times = [datetime.fromisoformat(str(item["observed_at"]).replace("Z", "+00:00")) for item in observations]
        except ValueError:
            return None
        if any(not math.isfinite(float(item["gmp_value"])) or now - timestamp.astimezone(timezone.utc) > timedelta(hours=24) or timestamp.astimezone(IST).date() != now.astimezone(IST).date() for item, timestamp in zip(observations, times)):
            return None
        return {
            "low": min(float(item["gmp_value"]) for item in observations),
            "high": max(float(item["gmp_value"]) for item in observations),
            "sources": [{"name": item["source_name"], "value": float(item["gmp_value"]), "source_url": item["source_url"], "observed_at": item["observed_at"]} for item in observations],
            "disclaimer": "Unofficial market sentiment. Not used in research confidence, grade, or verdict.",
        }

    def _trim_text(self, text: str) -> str:
        cleaned = "\n".join(line.strip() for line in text.splitlines() if line.strip())
        if not cleaned:
            raise IpoResearchError("The offer document did not contain readable text.")
        return cleaned[:MAX_DOCUMENT_CHARS]

    def _rules_dossier(self, issuer: str, source_url: str, text: str) -> Dict[str, Any]:
        lower = text.lower()
        risk_lines = [
            line for line in text.splitlines()
            if re.search(r"risk factor|litigation|indebtedness|contingent|pledge", line, re.I)
        ][:8]
        use_of_proceeds = self._section_excerpt(text, r"objects of the offer|objects of the issue")
        business = self._section_excerpt(text, r"our business|business overview")
        financials = self._section_excerpt(text, r"restated financial|financial information")
        has_peer_data = "peer group" in lower or "peer comparison" in lower
        coverage = {
            "business": bool(business),
            "financials": bool(financials),
            "use_of_proceeds": bool(use_of_proceeds),
            "risks": bool(risk_lines),
            "peer_comparison": has_peer_data,
        }
        gaps = [name.replace("_", " ") for name, present in coverage.items() if not present]
        return {
            "issuer": issuer,
            "source_url": source_url,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "generated_by": "rules_only",
            "verdict": "REVIEW",
            "confidence": 0.35,
            "investment_case": "Primary-source dossier prepared for analyst review; no investment recommendation is made.",
            "business": business or "Not located in the supplied document excerpt.",
            "financials": financials or "Not located in the supplied document excerpt.",
            "use_of_proceeds": use_of_proceeds or "Not located in the supplied document excerpt.",
            "risks": risk_lines or ["Review the offer document's Risk Factors section."],
            "peer_comparison": "Peer comparison must be verified against the offer document.",
            "coverage": coverage,
            "data_gaps": gaps,
            "disclaimer": "Decision-support research only. Verify every conclusion against the linked offer document.",
        }

    def _apply_publication_gate(self, dossier: Dict[str, Any]) -> Dict[str, Any]:
        missing = [name for name in ("business", "financials", "risks") if not dossier.get("coverage", {}).get(name)]
        if not missing:
            dossier["research_status"] = "available"
            dossier["research_block_reason"] = None
            return dossier
        reason = "Official document coverage is insufficient for " + ", ".join(missing) + "."
        return {**dossier, **self._unavailable_dossier(reason), "coverage": dossier.get("coverage", {}), "data_gaps": dossier.get("data_gaps", [])}

    @staticmethod
    def _unavailable_dossier(reason: str) -> Dict[str, Any]:
        return {
            "research_status": "unavailable", "research_block_reason": reason,
            "verdict": "UNAVAILABLE", "confidence": 0.0,
            "investment_case": f"Research unavailable: {reason}",
            "business": "Unavailable from official documents.",
            "financials": "Unavailable from official documents.",
            "use_of_proceeds": "Unavailable from official documents.",
            "risks": ["Official-document evidence was insufficient for a publishable research verdict."],
            "coverage": {}, "data_gaps": ["official document coverage"],
            "disclaimer": "Decision-support research only. Verify every conclusion against the linked offer document.",
        }

    def _llm_dossier(self, issuer: str, source_url: str, text: str) -> Dict[str, Any]:
        system = (
            "You are an IPO research analyst. Use only the supplied official offer-document text. "
            "Do not provide personalized investment advice, price targets, or performance claims. "
            "Return JSON with verdict (REVIEW, WATCH, or PASS), confidence (0-1), investment_case, "
            "business, financials, use_of_proceeds, risks (array), peer_comparison, coverage (object), and data_gaps (array)."
        )
        prompt = f"Issuer: {issuer}\nOfficial source: {source_url}\n\nOffer-document text:\n{text}"
        payload = self._llm.complete_json(system, prompt, model=self._llm.digest_model, temperature=0.0)
        if not isinstance(payload, dict):
            raise LLMUnavailable("IPO analysis did not return an object")
        payload.update(
            {
                "issuer": issuer,
                "source_url": source_url,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "generated_by": "llm",
                "disclaimer": "Decision-support research only. Verify every conclusion against the linked offer document.",
            }
        )
        return payload

    @staticmethod
    def _section_excerpt(text: str, heading_pattern: str) -> str:
        match = re.search(heading_pattern, text, re.I)
        if not match:
            return ""
        start = match.start()
        excerpt = text[start : start + 1_400]
        return " ".join(excerpt.split())
