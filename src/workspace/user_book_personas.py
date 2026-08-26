"""Phase 2 — link user watchlists to paper fund personas (userbook_*)."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src.config import Config, PROJECT_ROOT, _deep_merge, get_config, get_persona_config
from src.journal.db import connect
from src.logging_setup import get_logger
from src.market.registry import get_market

logger = get_logger(__name__)

DEFAULT_TEMPLATE = "balanced"
ACTIVE_STATUSES = frozenset({"active", "provisioning"})
PERSONA_PREFIX = "userbook_"


class UserBookPersonaError(ValueError):
    """Activation or persona lookup failure."""


class UserBookPersonaNotFoundError(KeyError):
    """No persona linked to this book or id."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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

    def get_by_book(self, book_id: str, user_id: int) -> Optional[Dict[str, Any]]:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM user_book_personas WHERE book_id = ? AND user_id = ?",
                (book_id, user_id),
            ).fetchone()
        if not row:
            return None
        return self._hydrate(dict(row))

    def list_active_persona_ids(self) -> List[str]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT persona_id FROM user_book_personas WHERE status = 'active' ORDER BY created_at ASC"
            ).fetchall()
        return [str(row["persona_id"]) for row in rows]

    def create(
        self,
        *,
        book_id: str,
        user_id: int,
        persona_id: str,
        template_persona_id: str,
        frozen_allocation: Dict[str, Any],
        status: str = "active",
    ) -> Dict[str, Any]:
        row_id, now = str(uuid.uuid4()), _now()
        with self._connection() as conn:
            conn.execute(
                "INSERT INTO user_book_personas "
                "(id, book_id, user_id, persona_id, template_persona_id, status, frozen_allocation, provisioned_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row_id,
                    book_id,
                    user_id,
                    persona_id,
                    template_persona_id,
                    status,
                    json.dumps(frozen_allocation),
                    now if status == "active" else None,
                    now,
                ),
            )
            conn.commit()
        row = self.get_by_book(book_id, user_id)
        assert row is not None
        return row

    def update_status(self, book_id: str, user_id: int, status: str) -> None:
        with self._connection() as conn:
            cursor = conn.execute(
                "UPDATE user_book_personas SET status = ?, provisioned_at = COALESCE(provisioned_at, ?) "
                "WHERE book_id = ? AND user_id = ?",
                (status, _now(), book_id, user_id),
            )
            conn.commit()
        if cursor.rowcount == 0:
            raise UserBookPersonaNotFoundError("User book persona not found.")

    def _hydrate(self, row: Dict[str, Any]) -> Dict[str, Any]:
        row["frozen_allocation"] = json.loads(row.get("frozen_allocation") or "{}")
        return row


def active_userbook_persona_ids(config: Optional[Config] = None) -> List[str]:
    return UserBookPersonaRepository(config).list_active_persona_ids()


def userbook_persona_config(
    persona_id: str,
    *,
    settings_path: Optional[str] = None,
    base: Optional[Config] = None,
) -> Config:
    """Build runtime config for a DB-provisioned userbook persona."""
    base = base or get_config(settings_path)
    row = UserBookPersonaRepository(base).get_by_persona_id(persona_id)
    if row is None:
        raise ValueError(f"Unknown user book persona {persona_id!r}")

    frozen = row["frozen_allocation"]
    template_id = str(row.get("template_persona_id") or DEFAULT_TEMPLATE)
    template = get_persona_config(template_id, base=base, settings_path=settings_path)

    market_id = str(frozen.get("market_id") or "india_equity")
    market = get_market(market_id)
    universe_file = str(market.universe.get("file") or "data/nifty500.csv")
    if not universe_file.startswith("/"):
        universe_file = str(PROJECT_ROOT / universe_file)
    symbols = [str(s).upper() for s in frozen.get("symbols") or []]
    capital = float(frozen.get("starting_capital") or template.secrets.initial_capital)
    symbol_count = max(len(symbols), 1)

    merged = _deep_merge(
        dict(template.raw),
        {
            "universe": {
                "file": universe_file,
                "mode": "fixed",
                "symbols": symbols,
            },
            "execution": {"auto_approve": True},
            "fund": {
                "target_positions": min(symbol_count, 25),
                "max_positions": min(max(symbol_count, 5), 25),
            },
            "quant": {"max_candidates": min(max(symbol_count * 2, 10), 50)},
        },
    )
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
        if book.get("status") == "active_paper":
            existing = self.repo.get_by_book(book["id"], user_id)
            if existing and existing.get("status") == "active":
                raise UserBookPersonaError("This watchlist already has an active paper persona.")
        if not lines:
            raise UserBookPersonaError("Add at least one holding before activating a paper fund.")

        invested = sum(float(line.get("target_amount") or 0) for line in lines)
        if invested <= 0:
            raise UserBookPersonaError("Allocate capital to at least one holding before activating.")

        existing = self.repo.get_by_book(book["id"], user_id)
        if existing and existing.get("status") in ACTIVE_STATUSES:
            raise UserBookPersonaError("This watchlist already has an active paper persona.")

        try:
            get_persona_config(template_persona_id, base=self._config)
        except ValueError as exc:
            raise UserBookPersonaError(f"Unknown template persona: {template_persona_id}") from exc

        weights = {
            line["symbol"]: float(line.get("weight_pct") or 0)
            for line in lines
            if float(line.get("target_amount") or 0) > 0
        }
        symbols = list(weights.keys())
        persona_id = f"{PERSONA_PREFIX}{uuid.uuid4().hex[:12]}"
        frozen = {
            "book_id": book["id"],
            "book_name": book["name"],
            "owner_user_id": user_id,
            "market_id": book["market_id"],
            "starting_capital": float(book["starting_capital"]),
            "symbols": symbols,
            "target_weights": weights,
            "template_persona_id": template_persona_id,
        }
        row = self.repo.create(
            book_id=book["id"],
            user_id=user_id,
            persona_id=persona_id,
            template_persona_id=template_persona_id,
            frozen_allocation=frozen,
            status="active",
        )
        logger.info("Activated user book persona %s for book %s", persona_id, book["id"])
        return row

    def pause_for_book(self, book_id: str, user_id: int) -> None:
        try:
            self.repo.update_status(book_id, user_id, "paused")
        except UserBookPersonaNotFoundError:
            return

    def persona_for_book(self, book_id: str, user_id: int) -> Dict[str, Any]:
        row = self.repo.get_by_book(book_id, user_id)
        if row is None:
            raise UserBookPersonaNotFoundError("No paper persona linked to this watchlist.")
        return row
