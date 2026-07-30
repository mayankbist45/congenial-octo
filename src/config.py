"""Configuration loading for the fund manager.

Policy lives in ``config/settings.yaml``; secrets live in ``.env``. Code reads
both through :func:`get_config` so that no module hardcodes a risk limit.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SETTINGS_PATH = PROJECT_ROOT / "config" / "settings.yaml"


def _as_bool(value: Optional[str], default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True)
class Secrets:
    """Credentials pulled from the environment."""

    angelone_api_key: str = ""
    angelone_client_code: str = ""
    angelone_pin: str = ""
    angelone_totp_secret: str = ""
    openai_api_key: str = ""
    openai_base_url: Optional[str] = None
    dry_run: bool = True
    initial_capital: float = 1_000_000.0
    # Kept with the secrets because a production DSN carries a password.
    database_url: Optional[str] = None

    @property
    def has_broker_credentials(self) -> bool:
        return all(
            [
                self.angelone_api_key,
                self.angelone_client_code,
                self.angelone_pin,
                self.angelone_totp_secret,
            ]
        )

    @property
    def has_llm_credentials(self) -> bool:
        return bool(self.openai_api_key)


@dataclass(frozen=True)
class Config:
    """Merged view of policy settings plus resolved paths and secrets."""

    raw: Dict[str, Any]
    secrets: Secrets
    root: Path = field(default=PROJECT_ROOT)

    def section(self, name: str) -> Dict[str, Any]:
        value = self.raw.get(name)
        if not isinstance(value, dict):
            return {}
        return value

    def get(self, dotted_path: str, default: Any = None) -> Any:
        """Fetch a nested setting, e.g. ``config.get("risk.stop_loss_pct")``."""
        node: Any = self.raw
        for part in dotted_path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def path(self, dotted_path: str, default: str = "") -> Path:
        """Resolve a configured relative path against the project root."""
        value = self.get(dotted_path, default)
        candidate = Path(str(value))
        if candidate.is_absolute():
            return candidate
        return self.root / candidate

    # Frequently used sections, exposed as properties for readability.
    @property
    def fund(self) -> Dict[str, Any]:
        return self.section("fund")

    @property
    def risk(self) -> Dict[str, Any]:
        return self.section("risk")

    @property
    def quant(self) -> Dict[str, Any]:
        return self.section("quant")

    @property
    def news(self) -> Dict[str, Any]:
        return self.section("news")

    @property
    def llm(self) -> Dict[str, Any]:
        return self.section("llm")

    @property
    def execution(self) -> Dict[str, Any]:
        return self.section("execution")

    @property
    def cadence(self) -> Dict[str, Any]:
        return self.section("cadence")

    @property
    def universe(self) -> Dict[str, Any]:
        return self.section("universe")

    @property
    def market_data(self) -> Dict[str, Any]:
        return self.section("market_data")

    @property
    def fundamentals(self) -> Dict[str, Any]:
        return self.section("fundamentals")

    @property
    def db_path(self) -> Path:
        """Where the SQLite journal lives, when SQLite is the backend."""
        return self.path("database.path", "data/fund.db")

    @property
    def db_url(self) -> str:
        """DSN for the journal.

        ``DATABASE_URL`` in the environment wins, so a deployment can point at a
        different server without editing tracked config. Otherwise
        ``database.url`` decides, and if that is blank the fund falls back to the
        SQLite file, which is what keeps a zero-install first run working.
        """
        if self.secrets.database_url:
            return self.secrets.database_url
        configured = self.get("database.url")
        if configured and str(configured).strip():
            return str(configured).strip()
        return f"sqlite://{self.db_path}"

    @property
    def cache_dir(self) -> Path:
        directory = self.root / "data" / "cache"
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    @property
    def briefing_dir(self) -> Path:
        directory = self.root / "data" / "briefings"
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def validate(self) -> List[str]:
        """Return human-readable problems with the configuration."""
        problems: List[str] = []

        weights = self.get("quant.factor_weights", {}) or {}
        if weights:
            total = sum(float(v) for v in weights.values())
            if abs(total - 1.0) > 0.001:
                problems.append(
                    f"quant.factor_weights must sum to 1.0, got {total:.3f}"
                )

        if self.get("risk.stop_loss_pct", -1) >= 0:
            problems.append("risk.stop_loss_pct must be negative")
        if self.get("risk.trailing_stop_pct", -1) >= 0:
            problems.append("risk.trailing_stop_pct must be negative")

        max_pos = float(self.get("risk.max_position_pct", 0) or 0)
        target_positions = int(self.get("fund.target_positions", 0) or 0)
        if max_pos and target_positions and max_pos * target_positions < 100:
            problems.append(
                f"risk.max_position_pct ({max_pos}) x fund.target_positions "
                f"({target_positions}) cannot reach a fully invested portfolio"
            )

        if float(self.get("risk.max_sector_pct", 0) or 0) < max_pos:
            problems.append("risk.max_sector_pct must be at least max_position_pct")

        if not self.path("universe.file").exists():
            problems.append(f"universe file not found: {self.get('universe.file')}")

        return problems


def load_secrets() -> Secrets:
    load_dotenv(PROJECT_ROOT / ".env")
    initial_capital_raw = os.getenv("INITIAL_CAPITAL_INR", "1000000")
    try:
        initial_capital = float(initial_capital_raw)
    except ValueError:
        initial_capital = 1_000_000.0

    base_url = os.getenv("OPENAI_BASE_URL") or None

    return Secrets(
        angelone_api_key=os.getenv("ANGELONE_API_KEY", "").strip(),
        angelone_client_code=os.getenv("ANGELONE_CLIENT_CODE", "").strip(),
        angelone_pin=os.getenv("ANGELONE_PIN", "").strip(),
        angelone_totp_secret=os.getenv("ANGELONE_TOTP_SECRET", "").strip(),
        openai_api_key=os.getenv("OPENAI_API_KEY", "").strip(),
        openai_base_url=base_url,
        dry_run=_as_bool(os.getenv("DRY_RUN"), default=True),
        initial_capital=initial_capital,
        database_url=(os.getenv("DATABASE_URL") or "").strip() or None,
    )


@lru_cache(maxsize=1)
def get_config(settings_path: Optional[str] = None) -> Config:
    path = Path(settings_path) if settings_path else DEFAULT_SETTINGS_PATH
    with open(path, "r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    return Config(raw=raw, secrets=load_secrets())
