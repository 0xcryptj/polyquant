"""
ClobExecutionProvider — execution provider backed by py-clob-client (default).

Wraps:
  execution.clob_client.place_market_order()
  execution.clob_client.cancel_order()
  execution.clob_client.get_open_orders()
  data.collector_polymarket.get_order_book()

This is the default path used in Phase 2 and Phase 3.
Set EXECUTION_PROVIDER=clob (the default) to use this provider.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from config.settings import settings
from execution.clob_client import (
    OrderResult,
    cancel_order as _cancel_order,
    get_open_orders as _get_open_orders,
    place_market_order,
)
from execution.providers.base import ExecutionProvider, MarketSnapshot

logger = logging.getLogger(__name__)


class ClobExecutionProvider(ExecutionProvider):
    """
    Execution provider wrapping the existing py-clob-client SDK path.

    The CLOB client is initialised lazily (first call to _get_clob()),
    so paper-trading startup does not require live CLOB credentials.
    """

    name = "clob"

    def __init__(self, clob_client: Any | None = None) -> None:
        # Accept a pre-initialised client (from Orchestrator's WalletBundle)
        # or initialise lazily on first use.
        self._clob = clob_client

    # ── Lazy CLOB client ──────────────────────────────────────────────────────

    def _get_clob(self) -> Any:
        if self._clob is None:
            from wallets.wallet_manager import initialize_wallet
            bundle     = initialize_wallet()
            self._clob = bundle.clob_client
        return self._clob

    # ── ExecutionProvider interface ───────────────────────────────────────────

    def list_markets(self) -> list[dict]:
        try:
            with open(settings.btc_markets_config_path) as f:
                return json.load(f)
        except Exception as exc:
            logger.warning("list_markets: could not load config: %s", exc)
            return []

    def get_orderbook(self, token_id: str) -> MarketSnapshot:
        try:
            from data.collector_polymarket import get_order_book
            snap = get_order_book(token_id)
            return MarketSnapshot(
                token_id = token_id,
                bid      = snap.best_bid or 0.0,
                ask      = snap.best_ask or 0.0,
                mid      = snap.mid_price or 0.0,
                spread   = (snap.best_ask or 0.0) - (snap.best_bid or 0.0),
            )
        except Exception as exc:
            logger.warning("get_orderbook(%s) failed: %s", token_id[:12], exc)
            return MarketSnapshot(token_id=token_id, bid=0, ask=0, mid=0, spread=0)

    def place_order(
        self,
        token_id:        str,
        direction:       str,
        size_usdc:       float,
        price:           float,
        *,
        idempotency_key: str | None = None,
    ) -> OrderResult:
        # idempotency_key is unused on the CLOB path —
        # py-clob-client handles deduplication server-side via order IDs.
        return place_market_order(
            clob_client = self._get_clob(),
            token_id    = token_id,
            direction   = direction,
            size_usdc   = size_usdc,
            price       = price,
        )

    def cancel_order(self, order_id: str) -> bool:
        return _cancel_order(self._get_clob(), order_id)

    def positions(self) -> list[dict]:
        return _get_open_orders(self._get_clob())

    # ── Risk gate ─────────────────────────────────────────────────────────────

    def can_execute(self) -> tuple[bool, str]:
        if settings.paper_trading:
            return True, ""   # paper mode is always ok
        # Live: verify CLOB is reachable before each cycle
        try:
            ok = self._get_clob().get_ok()
            if not ok:
                return False, "CLOB health check returned not-ok"
        except Exception as exc:
            return False, f"CLOB unreachable: {exc}"
        return True, ""

    def health(self) -> bool:
        try:
            self._get_clob().get_ok()
            return True
        except Exception:
            return False
