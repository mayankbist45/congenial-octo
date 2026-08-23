"""Source-first IPO discovery and research.

The service deliberately treats a prospectus as evidence, not as a trading
signal. It exposes the source URL and coverage gaps with every dossier so an
analyst can verify the conclusion before making an investment decision.
"""

from __future__ import annotations

import io
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from src.config import Config, get_config
from src.llm import LLMClient, LLMUnavailable
from src.logging_setup import get_logger

logger = get_logger(__name__)

SEBI_PUBLIC_ISSUES_URL = (
    "https://www.sebi.gov.in/sebiweb/home/HomeAction.do?doListing=yes&sid=3&smid=10&ssid=15"
)
DOCUMENT_TIMEOUT_SECONDS = 25
MAX_DOCUMENT_CHARS = 90_000


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
        dossier = self._rules_dossier(issuer, source_url, excerpt)
        if self._llm.available:
            try:
                dossier = self._llm_dossier(issuer, source_url, excerpt)
            except LLMUnavailable as exc:
                logger.warning("IPO LLM analysis unavailable for %s: %s", issuer, exc)
        return dossier

    def _download_document(self, source_url: str) -> str:
        response = self._session.get(
            source_url,
            timeout=DOCUMENT_TIMEOUT_SECONDS,
            headers={"User-Agent": "Nivelo-IPO-Research/1.0"},
        )
        response.raise_for_status()
        content_type = response.headers.get("content-type", "").lower()
        if "pdf" in content_type or source_url.lower().split("?")[0].endswith(".pdf"):
            try:
                from pypdf import PdfReader
            except ImportError as exc:  # pragma: no cover - dependency guard
                raise IpoResearchError("PDF support is unavailable; install pypdf.") from exc
            reader = PdfReader(io.BytesIO(response.content))
            return "\n".join(page.extract_text() or "" for page in reader.pages)
        return BeautifulSoup(response.text, "html.parser").get_text("\n", strip=True)

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
