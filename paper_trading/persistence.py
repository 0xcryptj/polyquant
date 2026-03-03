"""
SQLite persistence layer for paper trading.

Schema:
  trades              — all paper trades (open and closed)
  balance             — current paper USDC balance (single row)
  adaptive_params     — learner-adjusted trading parameters
  learning_snapshots  — periodic performance snapshots for trend tracking
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config.settings import PROJECT_ROOT

DB_PATH = PROJECT_ROOT / "paper_trading" / "paper_trades.db"

# ── Connection ────────────────────────────────────────────────────────────────
# Single global SQLite connection (check_same_thread=False). Safe when only the
# main thread and one APScheduler/engine thread access the DB; SQLite serializes.
# For additional threads, consider a connection per thread or a small pool.

def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")  # 10s wait on lock instead of immediate failure
    return conn


_conn: sqlite3.Connection | None = None


def get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = _connect()
    return _conn


# ── Schema ────────────────────────────────────────────────────────────────────

def init_db() -> None:
    """Create tables if they don't exist."""
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS trades (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id        TEXT    NOT NULL,
            token_id        TEXT    NOT NULL,
            direction       TEXT    NOT NULL,
            entry_price     REAL    NOT NULL,
            size_usdc       REAL    NOT NULL,
            shares          REAL    NOT NULL,
            model_prob      REAL    NOT NULL,
            edge            REAL    NOT NULL,
            btc_price_entry REAL    NOT NULL,
            opened_at       TEXT    NOT NULL,
            resolved_at     TEXT,
            btc_price_exit  REAL,
            exit_price      REAL,
            pnl             REAL,
            status          TEXT    NOT NULL DEFAULT 'open',
            features        TEXT
        );

        CREATE TABLE IF NOT EXISTS balance (
            id             INTEGER PRIMARY KEY CHECK (id = 1),
            usdc           REAL    NOT NULL,
            starting_usdc  REAL,
            updated_at     TEXT    NOT NULL
        );

        CREATE TABLE IF NOT EXISTS adaptive_params (
            key         TEXT PRIMARY KEY,
            value       REAL    NOT NULL,
            updated_at  TEXT    NOT NULL
        );

        CREATE TABLE IF NOT EXISTS learning_snapshots (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_at     TEXT    NOT NULL,
            n_trades        INTEGER,
            win_rate        REAL,
            avg_edge        REAL,
            avg_pnl         REAL,
            brier_score     REAL,
            min_edge_used   REAL,
            kelly_used      REAL,
            notes           TEXT
        );
    """)
    conn.commit()
    # Migration: add starting_usdc if missing (existing DBs)
    try:
        conn.execute("ALTER TABLE balance ADD COLUMN starting_usdc REAL")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # column already exists
    # Backfill starting_usdc for existing rows (NULL -> 1000 default)
    conn.execute(
        "UPDATE balance SET starting_usdc = 1000.0 WHERE starting_usdc IS NULL OR starting_usdc = 0"
    )
    conn.commit()


# ── Balance ───────────────────────────────────────────────────────────────────

def get_balance() -> float | None:
    """Return current USDC balance, or None if no row exists."""
    row = get_conn().execute("SELECT usdc FROM balance WHERE id = 1").fetchone()
    return float(row["usdc"]) if row else None


def get_balance_full() -> dict[str, float] | None:
    """Return {usdc, starting_usdc} or None if no row exists. Used for accurate PnL on restore."""
    row = get_conn().execute(
        "SELECT usdc, COALESCE(starting_usdc, usdc, 1000.0) as starting_usdc FROM balance WHERE id = 1"
    ).fetchone()
    if not row:
        return None
    return {"usdc": float(row["usdc"]), "starting_usdc": float(row["starting_usdc"])}


def set_balance(usdc: float, starting_usdc: float | None = None) -> None:
    """
    Persist balance. If starting_usdc is provided (e.g. on /reset), update both.
    Otherwise only update usdc — bankroll baseline is preserved.
    """
    now = _now()
    conn = get_conn()
    if starting_usdc is not None:
        conn.execute(
            "INSERT INTO balance (id, usdc, starting_usdc, updated_at) VALUES (1, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET usdc=excluded.usdc, starting_usdc=excluded.starting_usdc, "
            "updated_at=excluded.updated_at",
            (usdc, starting_usdc, now),
        )
    else:
        conn.execute(
            "INSERT INTO balance (id, usdc, starting_usdc, updated_at) VALUES (1, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET usdc=excluded.usdc, updated_at=excluded.updated_at",
            (usdc, usdc, now),  # new row: starting_usdc=usdc; update: starting_usdc unchanged
        )
    conn.commit()


# ── Trades ────────────────────────────────────────────────────────────────────

def insert_trade(
    order_id: str,
    token_id: str,
    direction: str,
    entry_price: float,
    size_usdc: float,
    shares: float,
    model_prob: float,
    edge: float,
    btc_price_entry: float,
    features: dict[str, float] | None = None,
) -> int:
    """Insert a new open trade. Returns the row id."""
    now = _now()
    cur = get_conn().execute(
        """INSERT INTO trades
           (order_id, token_id, direction, entry_price, size_usdc, shares,
            model_prob, edge, btc_price_entry, opened_at, status, features)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)""",
        (
            order_id, token_id, direction, entry_price, size_usdc, shares,
            model_prob, edge, btc_price_entry, now,
            json.dumps(features) if features else None,
        ),
    )
    get_conn().commit()
    return cur.lastrowid  # type: ignore[return-value]


def insert_trade_and_set_balance(
    new_balance: float,
    order_id: str,
    token_id: str,
    direction: str,
    entry_price: float,
    size_usdc: float,
    shares: float,
    model_prob: float,
    edge: float,
    btc_price_entry: float,
    features: dict[str, float] | None = None,
) -> int:
    """Insert a new open trade and update balance in one transaction. Returns the trade row id."""
    now = _now()
    conn = get_conn()
    conn.execute("BEGIN")
    try:
        cur = conn.execute(
            """INSERT INTO trades
               (order_id, token_id, direction, entry_price, size_usdc, shares,
                model_prob, edge, btc_price_entry, opened_at, status, features)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)""",
            (
                order_id, token_id, direction, entry_price, size_usdc, shares,
                model_prob, edge, btc_price_entry, now,
                json.dumps(features) if features else None,
            ),
        )
        trade_id = cur.lastrowid
        conn.execute(
            "INSERT INTO balance (id, usdc, starting_usdc, updated_at) VALUES (1, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET usdc=excluded.usdc, updated_at=excluded.updated_at",
            (new_balance, new_balance, now),
        )
        conn.commit()
        return trade_id  # type: ignore[return-value]
    except Exception:
        conn.rollback()
        raise


def resolve_trade(
    trade_id: int,
    btc_price_exit: float,
    exit_price: float,   # 1.0 = win, 0.0 = loss
    pnl: float,
    status: str,         # 'won' or 'lost'
) -> None:
    """Mark an open trade as resolved."""
    now = _now()
    get_conn().execute(
        """UPDATE trades SET
               resolved_at = ?,
               btc_price_exit = ?,
               exit_price = ?,
               pnl = ?,
               status = ?
           WHERE id = ?""",
        (now, btc_price_exit, exit_price, pnl, status, trade_id),
    )
    get_conn().commit()


def resolve_trade_and_set_balance(
    trade_id: int,
    btc_price_exit: float,
    exit_price: float,
    pnl: float,
    status: str,
    new_balance: float,
) -> None:
    """Mark an open trade as resolved and update balance in one transaction."""
    now = _now()
    conn = get_conn()
    conn.execute("BEGIN")
    try:
        conn.execute(
            """UPDATE trades SET
                   resolved_at = ?,
                   btc_price_exit = ?,
                   exit_price = ?,
                   pnl = ?,
                   status = ?
               WHERE id = ?""",
            (now, btc_price_exit, exit_price, pnl, status, trade_id),
        )
        conn.execute(
            "INSERT INTO balance (id, usdc, starting_usdc, updated_at) VALUES (1, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET usdc=excluded.usdc, updated_at=excluded.updated_at",
            (new_balance, new_balance, now),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def get_open_trades() -> list[dict[str, Any]]:
    rows = get_conn().execute(
        "SELECT * FROM trades WHERE status = 'open' ORDER BY opened_at ASC"
    ).fetchall()
    return [dict(r) for r in rows]


def get_recent_trades(n: int = 20) -> list[dict[str, Any]]:
    rows = get_conn().execute(
        "SELECT * FROM trades WHERE status != 'open' ORDER BY resolved_at DESC LIMIT ?",
        (n,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_all_closed_trades() -> list[dict[str, Any]]:
    rows = get_conn().execute(
        "SELECT * FROM trades WHERE status IN ('won', 'lost') ORDER BY resolved_at ASC"
    ).fetchall()
    return [dict(r) for r in rows]


def get_trade_count() -> dict[str, int]:
    row = get_conn().execute(
        """SELECT
               COUNT(*) as total,
               COALESCE(SUM(CASE WHEN status = 'won'  THEN 1 ELSE 0 END), 0) as wins,
               COALESCE(SUM(CASE WHEN status = 'lost' THEN 1 ELSE 0 END), 0) as losses,
               COALESCE(SUM(CASE WHEN status = 'open' THEN 1 ELSE 0 END), 0) as open
           FROM trades"""
    ).fetchone()
    return dict(row) if row else {"total": 0, "wins": 0, "losses": 0, "open": 0}


def get_dashboard_data(n_closed: int = 25) -> dict:
    """Batch fetch for dashboard: balance, counts, open trades, recent closed. Fewer round-trips."""
    conn = get_conn()
    bal = conn.execute(
        "SELECT usdc, COALESCE(starting_usdc, usdc, 1000.0) as starting_usdc FROM balance WHERE id = 1"
    ).fetchone()
    balance = float(bal["usdc"]) if bal else 1000.0
    starting = float(bal["starting_usdc"]) if bal else 1000.0

    counts = conn.execute(
        """SELECT COUNT(*) as total,
                  COALESCE(SUM(CASE WHEN status='won' THEN 1 ELSE 0 END), 0) as wins,
                  COALESCE(SUM(CASE WHEN status='lost' THEN 1 ELSE 0 END), 0) as losses,
                  COALESCE(SUM(CASE WHEN status='open' THEN 1 ELSE 0 END), 0) as open
           FROM trades"""
    ).fetchone()
    counts = dict(counts) if counts else {"total": 0, "wins": 0, "losses": 0, "open": 0}

    today = datetime.now(timezone.utc).date().isoformat()
    daily = conn.execute(
        """SELECT COUNT(*) as n, COALESCE(SUM(pnl), 0) as pnl FROM trades
           WHERE status IN ('won','lost') AND resolved_at IS NOT NULL AND date(resolved_at)=?""",
        (today,),
    ).fetchone()
    trades_today = int(daily["n"]) if daily else 0
    daily_pnl = float(daily["pnl"]) if daily else 0.0

    opens = [dict(r) for r in conn.execute(
        "SELECT * FROM trades WHERE status='open' ORDER BY opened_at ASC"
    ).fetchall()]
    closed = [dict(r) for r in conn.execute(
        "SELECT * FROM trades WHERE status != 'open' ORDER BY resolved_at DESC LIMIT ?",
        (n_closed,),
    ).fetchall()]

    pnl_row = conn.execute(
        "SELECT COALESCE(SUM(pnl), 0) as s FROM trades WHERE status IN ('won', 'lost')"
    ).fetchone()
    all_time_pnl = float(pnl_row["s"]) if pnl_row else 0.0

    locked = sum(float(t.get("size_usdc") or 0) for t in opens)
    total_equity = balance + locked
    total_pnl = total_equity - starting
    wr = (counts["wins"] or 0) / max((counts["wins"] or 0) + (counts["losses"] or 0), 1)
    return {
        "balance": balance,
        "starting_balance": starting,
        "locked_in_positions": locked,
        "total_equity": total_equity,
        "return_pct": 100 * (total_equity / starting - 1) if starting else 0,
        "total_pnl": total_pnl,
        "all_time_pnl": all_time_pnl,
        "n_open": counts["open"],
        "n_wins": counts["wins"],
        "n_losses": counts["losses"],
        "n_total": counts["total"],
        "win_rate": wr,
        "daily_pnl": daily_pnl,
        "trades_today": trades_today,
        "opens": opens,
        "closed": closed,
    }


def get_daily_trade_stats() -> dict[str, float | int]:
    """Return trades_today (count) and daily_pnl (sum of pnl for trades resolved today UTC)."""
    today_utc = datetime.now(timezone.utc).date().isoformat()
    row = get_conn().execute(
        """SELECT
               COUNT(*) as trades_today,
               COALESCE(SUM(pnl), 0) as daily_pnl
           FROM trades
           WHERE status IN ('won', 'lost')
             AND resolved_at IS NOT NULL
             AND date(resolved_at) = ?""",
        (today_utc,),
    ).fetchone()
    if not row:
        return {"trades_today": 0, "daily_pnl": 0.0}
    return {"trades_today": int(row["trades_today"]), "daily_pnl": float(row["daily_pnl"])}


# ── Adaptive Parameters ───────────────────────────────────────────────────────

def get_param(key: str, default: float) -> float:
    row = get_conn().execute(
        "SELECT value FROM adaptive_params WHERE key = ?", (key,)
    ).fetchone()
    return float(row["value"]) if row else default


def set_param(key: str, value: float) -> None:
    now = _now()
    get_conn().execute(
        "INSERT INTO adaptive_params (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, value, now),
    )
    get_conn().commit()


def get_all_params() -> dict[str, float]:
    rows = get_conn().execute("SELECT key, value FROM adaptive_params").fetchall()
    return {r["key"]: float(r["value"]) for r in rows}


# ── Learning Snapshots ────────────────────────────────────────────────────────

def save_snapshot(
    n_trades: int,
    win_rate: float,
    avg_edge: float,
    avg_pnl: float,
    brier_score: float,
    min_edge_used: float,
    kelly_used: float,
    notes: str = "",
) -> None:
    get_conn().execute(
        """INSERT INTO learning_snapshots
           (snapshot_at, n_trades, win_rate, avg_edge, avg_pnl, brier_score,
            min_edge_used, kelly_used, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (_now(), n_trades, win_rate, avg_edge, avg_pnl, brier_score,
         min_edge_used, kelly_used, notes),
    )
    get_conn().commit()


def get_recent_snapshots(n: int = 10) -> list[dict[str, Any]]:
    rows = get_conn().execute(
        "SELECT * FROM learning_snapshots ORDER BY snapshot_at DESC LIMIT ?", (n,)
    ).fetchall()
    return [dict(r) for r in rows]


# ── Trade Log (audit trail) ───────────────────────────────────────────────────

TRADE_LOG_PATH = DB_PATH.parent / "trade_log.jsonl"


def log_trade_open(
    trade_id: int,
    order_id: str,
    token_id: str,
    direction: str,
    entry_price: float,
    size_usdc: float,
    shares: float,
    btc_price_entry: float,
    model_prob: float,
    edge: float,
    simulated: bool = False,
) -> None:
    """Append trade-open event to audit log (JSONL)."""
    try:
        record = {
            "event": "open",
            "trade_id": trade_id,
            "order_id": order_id,
            "token_id": token_id,
            "direction": direction,
            "entry_price": entry_price,
            "size_usdc": size_usdc,
            "shares": shares,
            "btc_price_entry": btc_price_entry,
            "model_prob": model_prob,
            "edge": edge,
            "simulated": simulated,
            "timestamp": _now(),
        }
        with open(TRADE_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except OSError:
        pass  # best-effort, don't fail trade on log write


def log_trade_resolve(
    trade_id: int,
    token_id: str,
    direction: str,
    status: str,
    entry_price: float,
    exit_price: float,
    pnl: float,
    btc_price_entry: float,
    btc_price_exit: float,
    size_usdc: float,
    resolved_at: str,
) -> None:
    """Append trade-resolve event with outcome to audit log (JSONL)."""
    try:
        record = {
            "event": "resolve",
            "trade_id": trade_id,
            "token_id": token_id,
            "direction": direction,
            "status": status,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "pnl": pnl,
            "btc_price_entry": btc_price_entry,
            "btc_price_exit": btc_price_exit,
            "size_usdc": size_usdc,
            "resolved_at": resolved_at,
            "timestamp": _now(),
        }
        with open(TRADE_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except OSError:
        pass  # best-effort


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
