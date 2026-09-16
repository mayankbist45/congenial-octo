"""User-owned paper fund personas (userbook_*) spawned from watchlists."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src.config import Config, PROJECT_ROOT, _deep_merge, get_config, get_persona_config
from src.journal.db import connect
from src.logging_setup import get_logger
from src.market.registry import get_market
from src.workspace.entitlements import EntitlementsService, QuotaExceeded

logger = get_logger(__name__)

DEFAULT_TEMPLATE = "balanced"
ACTIVE_STATUSES = frozenset({"active", "provisioning"})
PERSONA_PREFIX = "userbook_"
PERSONA_LIMIT = 5


class UserBookPersonaError(ValueError):
    """Activation or persona lookup failure."""


class UserBookPersonaNotFoundError(KeyError):
    """No persona linked to this book or id."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def persona_universe_path(market_id: str) -> str:
    """Full investable universe file for user personas (not Nifty 50 fund default)."""
    market = get_market(market_id)
    universe_file = str(
        market.universe.get("persona_file")
        or market.universe.get("watchlist_file")
        or market.universe.get("file")
        or "data/nse_equity_all.csv"
    )
    if universe_file.startswith("/"):
        return universe_file
    return str(PROJECT_ROOT / universe_file)


class UserBookPersonaRepository:
    def __init__(self, config: Optional[Config] = None):
        self._config = config or get_config()

    class _ConnectionContext:
        def __init__(self, config: Config):
            self.conn = connect(config)

        def __enter__(self):
            return self.conn

        def __exit__(self, *_):
            self.conn.close()

    def _connection(self):
        return self._ConnectionContext(self._config)

    def get_by_persona_id(self, persona_id: str) -> Optional[Dict[str, Any]]:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM user_book_personas WHERE persona_id = ?",
                (persona_id,),
            ).fetchone()
        if not row:
            return None
        return self._hydrate(dict(row))

    def get_active_by_source_book(self, book_id: str, user_id: int) -> Optional[Dict[str, Any]]:
        """Optional lookup: active persona originally seeded from a watchlist."""
        for row in self.list_for_user(user_id, active_only=True):
            meta = row.get("provision_meta") or {}
            if str(meta.get("source_book_id") or "") == str(book_id):
                return row
        return None

    def get_by_book(self, book_id: str, user_id: int) -> Optional[Dict[str, Any]]:
        return self.get_active_by_source_book(book_id, user_id)

    def list_for_user(self, user_id: int, *, active_only: bool = False) -> List[Dict[str, Any]]:
        query = "SELECT * FROM user_book_personas WHERE user_id = ?"
        params: List[Any] = [user_id]
        if active_only:
            query += " AND status = 'active'"
        query += " ORDER BY created_at DESC"
        with self._connection() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
        return [self._hydrate(dict(row)) for row in rows]

    def list_active_for_user(self, user_id: int) -> List[Dict[str, Any]]:
        return self.list_for_user(user_id, active_only=True)

    def list_active_persona_ids(self) -> List[str]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT persona_id FROM user_book_personas WHERE status = 'active' ORDER BY created_at ASC"
            ).fetchall()
        return [str(row["persona_id"]) for row in rows]

    def create(
        self,
        *,
        user_id: int,
        persona_id: str,
        template_persona_id: str,
        provision_meta: Dict[str, Any],
        status: str = "active",
    ) -> Dict[str, Any]:
        row_id, now = str(uuid.uuid4()), _now()
        with self._connection() as conn:
            conn.execute(
                "INSERT INTO user_book_personas "
                "(id, user_id, persona_id, template_persona_id, status, frozen_allocation, "
                "pending_overrides, provisioned_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row_id,
                    user_id,
                    persona_id,
                    template_persona_id,
                    status,
                    json.dumps(provision_meta),
                    json.dumps({}),
                    now if status == "active" else None,
                    now,
                ),
            )
            conn.commit()
        row = self.get_by_persona_id(persona_id)
        assert row is not None
        return row

    def update_status(self, persona_id: str, user_id: int, status: str) -> None:
        with self._connection() as conn:
            cursor = conn.execute(
                "UPDATE user_book_personas SET status = ?, provisioned_at = COALESCE(provisioned_at, ?) "
                "WHERE persona_id = ? AND user_id = ?",
                (status, _now(), persona_id, user_id),
            )
            conn.commit()
        if cursor.rowcount == 0:
            raise UserBookPersonaNotFoundError("User book persona not found.")

    def pause_for_source_book(self, book_id: str, user_id: int) -> None:
        row = self.get_active_by_source_book(book_id, user_id)
        if row is None:
            return
        self.update_status(str(row["persona_id"]), user_id, "paused")

    def save_pending_overrides(
        self,
        persona_id: str,
        user_id: int,
        overrides: Dict[str, Any],
    ) -> Dict[str, Any]:
        row = self.get_by_persona_id(persona_id)
        if row is None or int(row.get("user_id") or 0) != int(user_id):
            raise UserBookPersonaNotFoundError("User book persona not found.")
        if row.get("status") != "active":
            raise UserBookPersonaError("Only active personas can be edited.")

        existing = dict(row.get("pending_overrides") or {})
        for key in ("template_persona_id", "starting_capital", "holdings"):
            if key in overrides and overrides[key] is not None:
                existing[key] = overrides[key]
        existing["updated_at"] = _now()
        with self._connection() as conn:
            conn.execute(
                "UPDATE user_book_personas SET pending_overrides = ? WHERE persona_id = ? AND user_id = ?",
                (json.dumps(existing), persona_id, user_id),
            )
            conn.commit()
        updated = self.get_by_persona_id(persona_id)
        assert updated is not None
        return updated

    def clear_pending_overrides(self, persona_id: str) -> None:
        with self._connection() as conn:
            conn.execute(
                "UPDATE user_book_personas SET pending_overrides = ? WHERE persona_id = ?",
                (json.dumps({}), persona_id),
            )
            conn.commit()

    def _hydrate(self, row: Dict[str, Any]) -> Dict[str, Any]:
        row["provision_meta"] = json.loads(row.get("frozen_allocation") or "{}")
        row["frozen_allocation"] = row["provision_meta"]
        row["pending_overrides"] = json.loads(row.get("pending_overrides") or "{}")
        return row


def active_userbook_persona_ids(config: Optional[Config] = None) -> List[str]:
    return UserBookPersonaRepository(config).list_active_persona_ids()


def userbook_persona_config(
    persona_id: str,
    *,
    settings_path: Optional[str] = None,
    base: Optional[Config] = None,
    row: Optional[Dict[str, Any]] = None,
) -> Config:
    """Build runtime config for a DB-provisioned userbook persona."""
    base = base or get_config(settings_path)
    if row is None:
        row = UserBookPersonaRepository(base).get_by_persona_id(persona_id)
    if row is None:
        raise ValueError(f"Unknown user book persona {persona_id!r}")

    provision = row["provision_meta"]
    pending = row.get("pending_overrides") or {}
    template_id = str(
        pending.get("template_persona_id")
        or row.get("template_persona_id")
        or provision.get("template_persona_id")
        or DEFAULT_TEMPLATE
    )
    template = get_persona_config(template_id, base=base, settings_path=settings_path)

    market_id = str(provision.get("market_id") or "india_equity")
    universe_file = persona_universe_path(market_id)
    capital = float(
        pending.get("starting_capital")
        or provision.get("starting_capital")
        or template.secrets.initial_capital
    )

    template_raw = {
        key: value for key, value in template.raw.items() if key not in {"universe", "personas"}
    }
    merged = _deep_merge(
        template_raw,
        {
            "universe": {"file": universe_file},
            "execution": {"auto_approve": True},
        },
    )
    if pending.get("holdings"):
        merged.setdefault("user_persona", {})["mandate_weights"] = pending["holdings"]

    secrets = template.secrets.with_capital(capital)
    return Config(raw=merged, secrets=secrets, root=base.root, persona_id=persona_id)


class UserBookPersonaProvisioner:
    def __init__(self, config: Optional[Config] = None, repo: Optional[UserBookPersonaRepository] = None):
        self._config = config or get_config()
        self.repo = repo or UserBookPersonaRepository(self._config)

    def activate(
        self,
        user_id: int,
        book: Dict[str, Any],
        lines: List[Dict[str, Any]],
        *,
        template_persona_id: str = DEFAULT_TEMPLATE,
    ) -> Dict[str, Any]:
        if book.get("status") == "archived":
            raise UserBookPersonaError("Archived watchlists cannot be activated.")
        if not lines:
            raise UserBookPersonaError("Add at least one holding before activating a paper fund.")

        invested = sum(float(line.get("target_amount") or 0) for line in lines)
        if invested <= 0:
            raise UserBookPersonaError("Allocate capital to at least one holding before activating.")

        try:
            EntitlementsService(self._config).check_quota(user_id, "active_funds")
        except QuotaExceeded as exc:
            raise UserBookPersonaError(
                f"Maximum of {exc.limit} active personas reached."
            ) from exc

        try:
            get_persona_config(template_persona_id, base=self._config)
        except ValueError as exc:
            raise UserBookPersonaError(f"Unknown template persona: {template_persona_id}") from exc

        weights = {
            line["symbol"]: float(line.get("weight_pct") or 0)
            for line in lines
            if float(line.get("target_amount") or 0) > 0
        }
        persona_id = f"{PERSONA_PREFIX}{uuid.uuid4().hex[:12]}"
        provision_meta = {
            "source_book_id": book["id"],
            "book_name": book["name"],
            "owner_user_id": user_id,
            "market_id": book["market_id"],
            "starting_capital": float(book["starting_capital"]),
            "seed_symbols": list(weights.keys()),
            "seed_weights": weights,
            "template_persona_id": template_persona_id,
        }
        row = self.repo.create(
            user_id=user_id,
            persona_id=persona_id,
            template_persona_id=template_persona_id,
            provision_meta=provision_meta,
            status="active",
        )
        logger.info("Activated user book persona %s from watchlist %s", persona_id, book["id"])
        return row

    def pause_for_book(self, book_id: str, user_id: int) -> None:
        self.repo.pause_for_source_book(book_id, user_id)

    def persona_for_book(self, book_id: str, user_id: int) -> Dict[str, Any]:
        """Most recently created active persona seeded from this watchlist (if any)."""
        row = self.repo.get_active_by_source_book(book_id, user_id)
        if row is None:
            raise UserBookPersonaNotFoundError("No active paper persona linked to this watchlist.")
        return row

    def persona_for_user(self, persona_id: str, user_id: int) -> Dict[str, Any]:
        row = self.repo.get_by_persona_id(persona_id)
        if row is None or int(row.get("user_id") or 0) != int(user_id):
            raise UserBookPersonaNotFoundError("User persona not found.")
        return row

    def update_overrides(self, persona_id: str, user_id: int, **overrides: Any) -> Dict[str, Any]:
        return self.repo.save_pending_overrides(persona_id, user_id, overrides)


class UserBookPersonaService:
    """Workspace-facing operations for user-owned personas."""

    def __init__(self, config: Optional[Config] = None):
        self._config = config or get_config()
        self.repo = UserBookPersonaRepository(self._config)
        self.provisioner = UserBookPersonaProvisioner(self._config, self.repo)

    def list_personas(self, user_id: int) -> List[Dict[str, Any]]:
        return self.repo.list_for_user(user_id)

    def get_persona(self, user_id: int, persona_id: str) -> Dict[str, Any]:
        return self.provisioner.persona_for_user(persona_id, user_id)

    def update_persona(
        self,
        user_id: int,
        persona_id: str,
        *,
        template_persona_id: Optional[str] = None,
        starting_capital: Optional[float] = None,
        holdings: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        if template_persona_id is not None:
            try:
                get_persona_config(template_persona_id, base=self._config)
            except ValueError as exc:
                raise UserBookPersonaError(f"Unknown template persona: {template_persona_id}") from exc
        if starting_capital is not None and starting_capital <= 0:
            raise UserBookPersonaError("starting_capital must be positive.")
        overrides: Dict[str, Any] = {}
        if template_persona_id is not None:
            overrides["template_persona_id"] = template_persona_id
        if starting_capital is not None:
            overrides["starting_capital"] = float(starting_capital)
        if holdings is not None:
            cleaned = []
            for item in holdings:
                symbol = str(item.get("symbol") or "").strip().upper()
                if not symbol:
                    continue
                cleaned.append(
                    {
                        "symbol": symbol,
                        "weight_pct": float(item.get("weight_pct") or 0),
                    }
                )
            overrides["holdings"] = cleaned
        if not overrides:
            raise UserBookPersonaError("No editable fields were provided.")
        row = self.provisioner.update_overrides(persona_id, user_id, **overrides)
        return {
            "persona": row,
            "pending_overrides": row.get("pending_overrides") or {},
            "applies_on": "next_long_cycle",
        }
