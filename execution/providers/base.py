"""
ExecutionProvider — abstract base class for all order execution backends.

Implementations:
  clob.py            — py-clob-client SDK path (default)
  polymarket_cli.py  — subprocess CLI path (EXECUTION_PROVIDER=cli)

The provider is the ONLY gateway to order placement.
Before any order, TradingService calls provider.can_execute() as a risk gate.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

# Re-export for convenience
from execution.clob_client import OrderResult


@dataclass
class MarketSnapshot:
    """Minimal orderbook representation."""
    token_id:  str
    bid:       float
    ask:       float
    mid:       float
    spread:    float
    raw:       dict[str, Any] = field(default_factory=dict)


class ExecutionProvider(ABC):
    """
    Abstract order execution backend.

    All methods are synchronous and safe to call from a thread pool.
    Heavy I/O (HTTP, subprocess) must have timeouts.
    Implementations must never raise on can_execute() or health().
    """

    name: str = "base"

    # ── Required ──────────────────────────────────────────────────────────────

    @abstractmethod
    def list_markets(self) -> list[dict]:
        """
        Return the configured market list (from btc_markets.json or live fetch).
        Returns [] on any error.
        """

    @abstractmethod
    def get_orderbook(self, token_id: str) -> MarketSnapshot:
        """
        Fetch current orderbook for a token.

        Returns:
            MarketSnapshot with bid/ask/mid/spread.
            On error, returns a snapshot with all prices = 0.
        """

    @abstractmethod
    def place_order(
        self,
        token_id:        str,
        direction:       str,
        size_usdc:       float,
        price:           float,
        *,
        idempotency_key: str | None = None,
    ) -> OrderResult:
        """
        Place an order.

        Args:
            token_id:        Polymarket YES token ID.
            direction:       "YES" or "NO".
            size_usdc:       USDC amount to spend.
            price:           Expected execution price (0..1).
            idempotency_key: Optional key to prevent duplicate orders on retry.

        Returns:
            OrderResult with success status and order_id.
            Never raises — returns OrderResult(success=False, error=...) on failure.
        """

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool:
        """
        Cancel an open order by ID.

        Returns:
            True on success or paper mode, False on failure.
            Never raises.
        """

    @abstractmethod
    def positions(self) -> list[dict]:
        """
        Return currently open positions/orders.

        Returns:
            List of dicts. Returns [] on error.
        """

    # ── Optional / overridable ────────────────────────────────────────────────

    def can_execute(self) -> tuple[bool, str]:
        """
        Risk gate checked by TradingService BEFORE calling place_order().

        Override to add provider-specific preconditions:
          - minimum balance check
          - rate limit budget remaining
          - upstream health

        Returns:
            (True, "")         — execution permitted
            (False, "reason")  — execution blocked; reason will be alerted
        """
        return True, ""

    def health(self) -> bool:
        """
        Quick liveness check for SupervisorService.

        Returns True if the provider is operational. Should be fast.
        Never raises.
        """
        return True
