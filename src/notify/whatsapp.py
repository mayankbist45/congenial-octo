"""WhatsApp outbound via Twilio (official Cloud/sandbox API)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List, Optional, Sequence
from urllib.parse import quote

import requests
from dotenv import load_dotenv

from src.config import PROJECT_ROOT
from src.logging_setup import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class WhatsAppConfig:
    account_sid: str
    auth_token: str
    from_number: str  # whatsapp:+1...
    to_numbers: List[str]  # whatsapp:+91...
    enabled: bool = True

    @property
    def configured(self) -> bool:
        return bool(
            self.enabled
            and self.account_sid
            and self.auth_token
            and self.from_number
            and self.to_numbers
        )


def _normalize_whatsapp_address(raw: str) -> str:
    value = (raw or "").strip()
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


def load_whatsapp_config() -> WhatsAppConfig:
    load_dotenv(PROJECT_ROOT / ".env")
    enabled_raw = (os.getenv("WHATSAPP_ENABLED") or "true").strip().lower()
    enabled = enabled_raw in {"1", "true", "yes", "on"}
    to_raw = os.getenv("WHATSAPP_TO") or os.getenv("TWILIO_WHATSAPP_TO") or ""
    recipients = [
        _normalize_whatsapp_address(part)
        for part in to_raw.replace(";", ",").split(",")
        if part.strip()
    ]
    recipients = [r for r in recipients if r]
    return WhatsAppConfig(
        account_sid=(os.getenv("TWILIO_ACCOUNT_SID") or "").strip(),
        auth_token=(os.getenv("TWILIO_AUTH_TOKEN") or "").strip(),
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
            "WhatsApp not configured (need TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, "
            "TWILIO_WHATSAPP_FROM, WHATSAPP_TO); skipping send"
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
                auth=(cfg.account_sid, cfg.auth_token),
                timeout=30,
            )
        except requests.RequestException as exc:
            ok = False
            logger.error("WhatsApp send failed to %s: %s", to, exc)
            results.append({"to": to, "ok": False, "error": str(exc)})
            continue
        if response.status_code >= 300:
            ok = False
            logger.error(
                "WhatsApp Twilio error to %s: %s %s",
                to,
                response.status_code,
                response.text[:300],
            )
            results.append(
                {
                    "to": to,
                    "ok": False,
                    "status_code": response.status_code,
                    "error": response.text[:300],
                }
            )
            continue
        payload = response.json() if response.content else {}
        sid = payload.get("sid")
        logger.info("WhatsApp message sent to %s sid=%s", to, sid)
        results.append({"to": to, "ok": True, "sid": sid})

    return {"ok": ok, "skipped": False, "results": results}
