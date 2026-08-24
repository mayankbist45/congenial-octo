"""WhatsApp outbound via Twilio (official Cloud/sandbox API)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List, Optional, Tuple
from urllib.parse import quote

import requests
from dotenv import load_dotenv

from src.config import PROJECT_ROOT
from src.logging_setup import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class WhatsAppConfig:
    account_sid: str  # must be AC...
    auth_user: str  # Account SID or API Key SID (SK...)
    auth_password: str  # Auth Token or API Key Secret
    from_number: str  # whatsapp:+1...
    to_numbers: List[str]  # whatsapp:+91...
    enabled: bool = True

    @property
    def configured(self) -> bool:
        return bool(
            self.enabled
            and self.account_sid.startswith("AC")
            and self.auth_user
            and self.auth_password
            and self.from_number
            and self.to_numbers
        )


def _normalize_whatsapp_address(raw: str) -> str:
    value = (raw or "").strip().strip('"').strip("'")
    if not value:
        return ""
    if value.startswith("whatsapp:"):
        return value
    if value.startswith("+"):
        return f"whatsapp:{value}"
    digits = "".join(ch for ch in value if ch.isdigit())
    if digits:
        return f"whatsapp:+{digits}"
    return value


def _clean(raw: Optional[str]) -> str:
    return (raw or "").strip().strip('"').strip("'")


def _redact_address(address: str) -> str:
    """Keep delivery diagnostics useful without writing recipients to logs."""
    digits = "".join(ch for ch in address if ch.isdigit())
    return f"***{digits[-4:]}" if len(digits) >= 4 else "configured recipient"


def load_whatsapp_config() -> WhatsAppConfig:
    load_dotenv(PROJECT_ROOT / ".env", override=True)
    enabled_raw = (os.getenv("WHATSAPP_ENABLED") or "true").strip().lower()
    enabled = enabled_raw in {"1", "true", "yes", "on"}
    to_raw = os.getenv("WHATSAPP_TO") or os.getenv("TWILIO_WHATSAPP_TO") or ""
    recipients = [
        _normalize_whatsapp_address(part)
        for part in to_raw.replace(";", ",").split(",")
        if part.strip()
    ]
    recipients = [r for r in recipients if r]

    account_sid = _clean(os.getenv("TWILIO_ACCOUNT_SID"))
    api_key = _clean(os.getenv("TWILIO_API_KEY") or os.getenv("TWILIO_API_KEY_SID"))
    auth_token = _clean(
        os.getenv("TWILIO_AUTH_TOKEN") or os.getenv("TWILIO_API_SECRET") or os.getenv("TWILIO_API_KEY_SECRET")
    )

    # Common mistake: API Key SID (SK...) pasted into TWILIO_ACCOUNT_SID.
    if account_sid.startswith("SK") and not api_key:
        api_key = account_sid
        account_sid = _clean(os.getenv("TWILIO_PARENT_ACCOUNT_SID"))
        if not account_sid:
            logger.error(
                "TWILIO_ACCOUNT_SID is an API Key (SK…). Put the Account SID "
                "(starts with AC) in TWILIO_ACCOUNT_SID, and set "
                "TWILIO_API_KEY=SK… plus TWILIO_AUTH_TOKEN=<api key secret>."
            )

    if api_key.startswith("SK"):
        auth_user, auth_password = api_key, auth_token
    else:
        auth_user, auth_password = account_sid, auth_token

    return WhatsAppConfig(
        account_sid=account_sid,
        auth_user=auth_user,
        auth_password=auth_password,
        from_number=_normalize_whatsapp_address(
            os.getenv("TWILIO_WHATSAPP_FROM") or os.getenv("WHATSAPP_FROM") or ""
        ),
        to_numbers=recipients,
        enabled=enabled,
    )


def send_whatsapp_message(
    body: str,
    config: Optional[WhatsAppConfig] = None,
    *,
    dry_run: bool = False,
) -> dict:
    """Send ``body`` to every configured WhatsApp recipient via Twilio.

    Returns a status dict. Does not raise on HTTP errors — logs and records them.
    """
    cfg = config or load_whatsapp_config()
    if not cfg.configured:
        logger.warning(
            "WhatsApp not configured correctly. Need TWILIO_ACCOUNT_SID starting "
            "with AC, TWILIO_AUTH_TOKEN (or API key secret), TWILIO_WHATSAPP_FROM, "
            "WHATSAPP_TO. Skipping send."
        )
        return {"ok": False, "skipped": True, "reason": "not_configured"}

    if dry_run:
        logger.info(
            "WhatsApp dry-run: %d recipient(s), %d chars",
            len(cfg.to_numbers),
            len(body),
        )
        return {
            "ok": True,
            "dry_run": True,
            "recipients": cfg.to_numbers,
            "chars": len(body),
            "preview": body[:500],
        }

    url = (
        f"https://api.twilio.com/2010-04-01/Accounts/"
        f"{quote(cfg.account_sid)}/Messages.json"
    )
    results = []
    ok = True
    for to in cfg.to_numbers:
        try:
            response = requests.post(
                url,
                data={"From": cfg.from_number, "To": to, "Body": body},
                auth=(cfg.auth_user, cfg.auth_password),
                timeout=30,
            )
        except requests.RequestException as exc:
            ok = False
            logger.error("WhatsApp send failed to %s: %s", _redact_address(to), exc)
            results.append({"recipient": _redact_address(to), "ok": False, "error": str(exc)})
            continue
        if response.status_code >= 300:
            ok = False
            logger.error(
                "WhatsApp Twilio error to %s: %s %s",
                _redact_address(to),
                response.status_code,
                response.text[:300],
            )
            results.append(
                {
                    "recipient": _redact_address(to),
                    "ok": False,
                    "status_code": response.status_code,
                    "error": response.text[:300],
                }
            )
            continue
        payload = response.json() if response.content else {}
        sid = payload.get("sid")
        logger.info("WhatsApp message sent to %s sid=%s", _redact_address(to), sid)
        results.append({"recipient": _redact_address(to), "ok": True, "sid": sid})

    return {"ok": ok, "skipped": False, "results": results}
