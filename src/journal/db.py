"""SQLite schema and connection handling.

The database is the fund's institutional memory: every decision (taken or
rejected) with its rationale, every fill, and the daily NAV series. Raw SQL
over sqlite3 keeps the schema inspectable with any SQLite browser.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from src.config import Config, get_config
from src.logging_setup import get_logger

logger = get_logger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    is_deep_review INTEGER NOT NULL DEFAULT 0,
    llm_used INTEGER NOT NULL DEFAULT 0,
    memo TEXT,
    portfolio_value REAL,
    cash REAL,
    turnover_used_pct REAL,
    status TEXT NOT NULL DEFAULT 'running'
);

CREATE TABLE IF NOT EXISTS proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    created_at TEXT NOT NULL,
    symbol TEXT NOT NULL,
    company_name TEXT,
    sector TEXT,
    action TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    reference_price REAL,
    limit_price REAL,
    estimated_value REAL,
    rationale TEXT,
    thesis TEXT,
    target_price REAL,
    trigger_types TEXT,
    confidence REAL,
    warnings TEXT,
    source TEXT,
    -- pending -> approved/rejected -> executed/failed
    status TEXT NOT NULL DEFAULT 'pending',
    decided_at TEXT,
    decided_by TEXT,
    rejection_reason TEXT
);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id INTEGER REFERENCES proposals(id),
    executed_at TEXT NOT NULL,
    symbol TEXT NOT NULL,
    action TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    price REAL NOT NULL,
    value REAL NOT NULL,
    broker_order_id TEXT,
    status TEXT NOT NULL,
    error TEXT,
    dry_run INTEGER NOT NULL DEFAULT 0,
    realised_pnl REAL
);

CREATE TABLE IF NOT EXISTS position_meta (
    symbol TEXT PRIMARY KEY,
    entry_date TEXT,
    entry_price REAL,
    entry_pe REAL,
    peak_price REAL,
    thesis TEXT,
    target_price REAL,
    updated_at TEXT
);

-- What the fund held at the close of each run, with the metrics that justified
-- holding it. Lets the dashboard and any later review show exactly what the
-- manager saw on a given day without re-querying the broker or recomputing.
CREATE TABLE IF NOT EXISTS position_snapshots (
    run_date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    company_name TEXT,
    sector TEXT,
    quantity INTEGER NOT NULL,
    avg_cost REAL NOT NULL,
    last_price REAL NOT NULL,
    market_value REAL NOT NULL,
    weight_pct REAL,
    return_pct REAL,
    unrealised_pnl REAL,
    holding_days INTEGER,
    composite_score REAL,
    beta REAL,
    idiosyncratic_vol REAL,
    risk_contribution_pct REAL,
    pct_of_adv REAL,
    thesis TEXT,
    PRIMARY KEY (run_date, symbol)
);

CREATE TABLE IF NOT EXISTS nav_history (
    run_date TEXT PRIMARY KEY,
    total_value REAL NOT NULL,
    invested_value REAL NOT NULL,
    cash REAL NOT NULL,
    position_count INTEGER NOT NULL,
    benchmark_close REAL,
    daily_return_pct REAL,
    benchmark_return_pct REAL,
    realised_pnl REAL,
    unrealised_pnl REAL
);

CREATE TABLE IF NOT EXISTS risk_history (
    run_date TEXT PRIMARY KEY,
    portfolio_beta REAL,
    portfolio_vol REAL,
    idio_share_pct REAL,
    tracking_error_pct REAL,
    active_share_pct REAL,
    concentration_top5_pct REAL,
    avg_pairwise_correlation REAL,
    payload TEXT
);

CREATE TABLE IF NOT EXISTS news_digests (
    run_date TEXT PRIMARY KEY,
    macro_summary TEXT,
    market_summary TEXT,
    holdings_notes TEXT,
    watchlist_notes TEXT,
    flagged_symbols TEXT,
    article_count INTEGER,
    high_impact_count INTEGER,
    generated_by_llm INTEGER
);

CREATE INDEX IF NOT EXISTS idx_proposals_run ON proposals(run_id);
CREATE INDEX IF NOT EXISTS idx_proposals_status ON proposals(status);
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
CREATE INDEX IF NOT EXISTS idx_trades_executed ON trades(executed_at);
CREATE INDEX IF NOT EXISTS idx_runs_date ON runs(run_date);
CREATE INDEX IF NOT EXISTS idx_snapshots_date ON position_snapshots(run_date);
"""


def connect(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Open a connection with sane defaults and the schema applied."""
    path = db_path or get_config().db_path
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def session(db_path: Optional[Path] = None) -> Iterator[sqlite3.Connection]:
    """Transactional connection: commits on success, rolls back on error."""
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(config: Optional[Config] = None) -> Path:
    config = config or get_config()
    path = config.db_path
    conn = connect(path)
    conn.close()
    logger.info("Database ready at %s", path)
    return path
