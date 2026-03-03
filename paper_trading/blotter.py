"""
TradeBlotter — append-only structured audit log.

Every trade event (placed, filled, cancelled, vetoed, kill-switch) is
written as a newline-delimited JSON record to AUDIT_LOG_PATH
(default: paper_trading/audit.log).

Design:
  - Append-only: records are never modified or deleted
  - Thread-safe:  all writes go through a single file open+close
  - Lightweight:  no external dependencies, no background threads
  - Queryable:    tail() and today_summary() for dashboards and Telegram

Usage:
    from paper_trading.blotter import TradeBlotter
    blotter = TradeBlotter()
    blotter.record_order_placed(order_id="x", token_id="y", ...)
    blotter.record_order_filled(order_id="x", pnl=1.50, ...)
    blotter.today_summary()     # → {"orders_placed": 5, "total_pnl": 3.20, ...}
    blotter.tail(10)            # → last 10 records as list[dict]
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config.settings import settings

logger = logging.getLogger(__name__)


class TradeBlotter:
    """
    Append-only JSONL trade audit log.

    Each record has the schema:
        {"ts": "ISO-8601-UTC", "event": "order_placed|order_filled|...", ...fields}

    Event types:
        order_placed    — an order was submitted
        order_filled    — an order resolved (won/lost)
        order_cancelled — an order was cancelled
        kill_switch     — kill switch triggered
        provider_veto   — execution provider blocked an order
        learner_updated — adaptive learning ran
    """

    def __init__(self, path: str | Path | None = None) -> None:
        raw  = path or settings.audit_log_path
        self._path = Path(raw).expanduser()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    # ── Write helpers ─────────────────────────────────────────────────────────

    def _write(self, event_type: str, data: dict[str, Any]) -> None:
        record = {
            "ts":    datetime.now(timezone.utc).isoformat(),
            "event": event_type,
            **data,
        }
        line = json.dumps(record, default=str) + "\n"
        with self._lock:
            try:
                with open(self._path, "a", encoding="utf-8") as f:
                    f.write(line)
            except OSError as exc:
                logger.error("Blotter write failed: %s", exc)

    # ── Event recorders ───────────────────────────────────────────────────────

    def record_order_placed(
        self,
        order_id:  str,
        token_id:  str,
        direction: str,
        size_usdc: float,
        price:     float,
        edge:      float       = 0.0,
        provider:  str         = "clob",
        paper:     bool        = True,
        **extra:   Any,
    ) -> None:
        self._write("order_placed", {
            "order_id":  order_id,
            "token_id":  token_id,
            "direction": direction,
            "size_usdc": size_usdc,
            "price":     price,
            "edge":      edge,
            "provider":  provider,
            "paper":     paper,
            **extra,
        })

    def record_order_filled(
        self,
        order_id:   str,
        pnl:        float,
        exit_price: float,
        balance:    float,
        won:        bool  = False,
        **extra:    Any,
    ) -> None:
        self._write("order_filled", {
            "order_id":   order_id,
            "pnl":        pnl,
            "exit_price": exit_price,
            "balance":    balance,
            "won":        won,
            **extra,
        })

    def record_order_cancelled(
        self,
        order_id: str,
        reason:   str = "manual",
        **extra:  Any,
    ) -> None:
        self._write("order_cancelled", {
            "order_id": order_id,
            "reason":   reason,
            **extra,
        })

    def record_kill_switch(
        self,
        reason:  str,
        balance: float,
        **extra: Any,
    ) -> None:
        self._write("kill_switch", {
            "reason":  reason,
            "balance": balance,
            **extra,
        })

    def record_provider_veto(
        self,
        provider:  str,
        reason:    str,
        token_id:  str,
        size_usdc: float,
        **extra:   Any,
    ) -> None:
        self._write("provider_veto", {
            "provider":  provider,
            "reason":    reason,
            "token_id":  token_id,
            "size_usdc": size_usdc,
            **extra,
        })

    def record_learner_updated(
        self,
        n:        int,
        win_rate: float,
        **params: Any,
    ) -> None:
        self._write("learner_updated", {
            "n":        n,
            "win_rate": win_rate,
            **params,
        })

    # ── Read helpers ──────────────────────────────────────────────────────────

    def tail(self, n: int = 20) -> list[dict]:
        """Return the last n records, oldest-first."""
        if not self._path.exists():
            return []
        try:
            lines = self._path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        records: list[dict] = []
        for line in lines[-n:]:
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        return records

    def today_summary(self) -> dict[str, Any]:
        """
        Aggregate today's blotter events.

        Returns:
            {
                "date":            "YYYY-MM-DD",
                "orders_placed":   int,
                "orders_filled":   int,
                "orders_cancelled":int,
                "kills":           int,
                "vetoes":          int,
                "total_pnl":       float,
                "wins":            int,
                "losses":          int,
            }
        """
        today = datetime.now(timezone.utc).date().isoformat()
        records = self.tail(500)

        summary: dict[str, Any] = {
            "date":             today,
            "orders_placed":    0,
            "orders_filled":    0,
            "orders_cancelled": 0,
            "kills":            0,
            "vetoes":           0,
            "total_pnl":        0.0,
            "wins":             0,
            "losses":           0,
        }

        for r in records:
            if not (r.get("ts") or "").startswith(today):
                continue
            ev = r.get("event", "")
            if ev == "order_placed":
                summary["orders_placed"] += 1
            elif ev == "order_filled":
                summary["orders_filled"] += 1
                summary["total_pnl"]     += float(r.get("pnl", 0))
                if r.get("won"):
                    summary["wins"]   += 1
                else:
                    summary["losses"] += 1
            elif ev == "order_cancelled":
                summary["orders_cancelled"] += 1
            elif ev == "kill_switch":
                summary["kills"] += 1
            elif ev == "provider_veto":
                summary["vetoes"] += 1

        return summary

    def path(self) -> str:
        return str(self._path)
