"""Configuration loading for the fund manager.

Policy lives in ``config/settings.yaml``; secrets live in ``.env``. Code reads
both through :func:`get_config` so that no module hardcodes a risk limit.
"""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SETTINGS_PATH = PROJECT_ROOT / "config" / "settings.yaml"
DEFAULT_PERSONA = "balanced"


def _as_bool(value: Optional[str], default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


@dataclass(frozen=True)
class Secrets:
    """Credentials pulled from the environment."""

    angelone_api_key: str = ""
    angelone_client_code: str = ""
    angelone_pin: str = ""
    angelone_totp_secret: str = ""
    angelone_data2_api_key: str = ""
    angelone_data2_client_code: str = ""
    angelone_data2_pin: str = ""
    angelone_data2_totp_secret: str = ""
    alpaca_api_key: str = ""
    alpaca_secret_key: str = ""
    alpaca_paper: bool = True
    openai_api_key: str = ""
    openai_base_url: Optional[str] = None
    dry_run: bool = True
    initial_capital: float = 1_000_000.0
    # Kept with the secrets because a production DSN carries a password.
    database_url: Optional[str] = None

    def with_capital(self, capital: float) -> "Secrets":
        return Secrets(
            angelone_api_key=self.angelone_api_key,
            angelone_client_code=self.angelone_client_code,
            angelone_pin=self.angelone_pin,
            angelone_totp_secret=self.angelone_totp_secret,
            angelone_data2_api_key=self.angelone_data2_api_key,
            angelone_data2_client_code=self.angelone_data2_client_code,
            angelone_data2_pin=self.angelone_data2_pin,
            angelone_data2_totp_secret=self.angelone_data2_totp_secret,
            alpaca_api_key=self.alpaca_api_key,
            alpaca_secret_key=self.alpaca_secret_key,
            alpaca_paper=self.alpaca_paper,
            openai_api_key=self.openai_api_key,
            openai_base_url=self.openai_base_url,
            dry_run=self.dry_run,
            initial_capital=capital,
            database_url=self.database_url,
        )

    @property
    def has_secondary_candle_credentials(self) -> bool:
        return bool(self.angelone_data2_api_key.strip())

    def secondary_candle_credentials(self):
        from src.broker.angelone import AngelOneCredentials

        if not self.has_secondary_candle_credentials:
            return None
        return AngelOneCredentials(
            api_key=self.angelone_data2_api_key.strip(),
            client_code=(self.angelone_data2_client_code or self.angelone_client_code).strip(),
            pin=(self.angelone_data2_pin or self.angelone_pin).strip(),
            totp_secret=(self.angelone_data2_totp_secret or self.angelone_totp_secret).strip(),
            label="data2",
        )

    @property
    def has_alpaca_credentials(self) -> bool:
        return bool(self.alpaca_api_key and self.alpaca_secret_key)

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
    persona_id: str = field(default=DEFAULT_PERSONA)

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

    @property
    def broker_provider(self) -> str:
        """Active connector id: ``angelone`` (default) or ``alpaca``."""
        return str(
            self.get("broker.provider") or self.get("connectors.broker") or "angelone"
        ).lower()

    @property
    def market_region(self) -> str:
        return str(self.get("connectors.market_region", "IN")).upper()

    @property
    def intraday_dir(self) -> Path:
        directory = self.root / "data" / "intraday"
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

        if self.get("intraday.enabled"):
            if not self.path("universe.file").exists():
                problems.append(
                    f"universe file not found: {self.get('universe.file')}"
                )
            return problems

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
        angelone_data2_api_key=os.getenv("ANGELONE_DATA2_API_KEY", "").strip(),
        angelone_data2_client_code=os.getenv("ANGELONE_DATA2_CLIENT_CODE", "").strip(),
        angelone_data2_pin=os.getenv("ANGELONE_DATA2_PIN", "").strip(),
        angelone_data2_totp_secret=os.getenv("ANGELONE_DATA2_TOTP_SECRET", "").strip(),
        alpaca_api_key=os.getenv("ALPACA_API_KEY", "").strip(),
        alpaca_secret_key=os.getenv("ALPACA_SECRET_KEY", "").strip(),
        alpaca_paper=_as_bool(os.getenv("ALPACA_PAPER"), default=True),
        openai_api_key=os.getenv("OPENAI_API_KEY", "").strip(),
        openai_base_url=base_url,
        dry_run=_as_bool(os.getenv("DRY_RUN"), default=True),
        initial_capital=initial_capital,
        database_url=(os.getenv("DATABASE_URL") or "").strip() or None,
    )


def _load_persona_overlays(settings_dir: Path) -> Dict[str, Any]:
    personas: Dict[str, Any] = {}
    for name in ("personas_in.yaml", "personas_us.yaml"):
        overlay_path = settings_dir / name
        if not overlay_path.exists():
            continue
        with open(overlay_path, "r", encoding="utf-8") as handle:
            block = yaml.safe_load(handle) or {}
        block_personas = block.get("personas", block)
        if isinstance(block_personas, dict):
            personas.update(block_personas)
    return personas


@lru_cache(maxsize=1)
def get_config(settings_path: Optional[str] = None) -> Config:
    path = Path(settings_path) if settings_path else DEFAULT_SETTINGS_PATH
    with open(path, "r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    overlay_personas = _load_persona_overlays(path.parent)
    if overlay_personas:
        raw["personas"] = overlay_personas
    return Config(raw=raw, secrets=load_secrets())


def list_personas(settings_path: Optional[str] = None) -> List[str]:
    config = get_config(settings_path)
    personas = config.raw.get("personas", {})
    if not isinstance(personas, dict):
        return [DEFAULT_PERSONA]
    return list(personas.keys()) or [DEFAULT_PERSONA]


def enabled_personas(settings_path: Optional[str] = None) -> List[str]:
    config = get_config(settings_path)
    personas = config.raw.get("personas", {})
    if not isinstance(personas, dict):
        return [DEFAULT_PERSONA]
    return [
        pid for pid, pdef in personas.items()
        if isinstance(pdef, dict) and pdef.get("enabled", True)
    ] or [DEFAULT_PERSONA]


def get_persona_config(
    persona_id: str, settings_path: Optional[str] = None
) -> Config:
    base = get_config(settings_path)
    personas = base.raw.get("personas", {})
    if persona_id not in personas:
        available = ", ".join(personas.keys()) if personas else "(none defined)"
        raise ValueError(f"Unknown persona {persona_id!r}; available: {available}")

    persona_def = personas[persona_id]
    if not isinstance(persona_def, dict):
        raise ValueError(
            f"Persona {persona_id!r} must be a mapping, got {type(persona_def).__name__}"
        )

    meta_keys = {"enabled", "capital_inr", "capital_usd", "description", "strategy_description"}
    overrides = {k: v for k, v in persona_def.items() if k not in meta_keys}
    base_raw = {k: v for k, v in base.raw.items() if k != "personas"}
    merged = _deep_merge(base_raw, overrides)
    merged["personas"] = base.raw.get("personas", {})

    secrets = base.secrets
    capital = persona_def.get("capital_inr")
    if capital is None:
        capital = persona_def.get("capital_usd")
    if capital is not None:
        secrets = secrets.with_capital(float(capital))

    return Config(
        raw=merged,
        secrets=secrets,
        root=base.root,
        persona_id=persona_id,
    )


def persona_description(persona_id: str, settings_path: Optional[str] = None) -> str:
    base = get_config(settings_path)
    personas = base.raw.get("personas", {})
    pdef = personas.get(persona_id, {})
    return str(pdef.get("description", persona_id))
