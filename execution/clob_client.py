"""
py-clob-client order wrapper with retry logic and paper trading support.

All order placement goes through this module. In PAPER_TRADING mode,
orders are logged but not submitted to the exchange.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from config.settings import settings

logger = logging.getLogger(__name__)


@dataclass
class OrderResult:
    """Result of an order placement attempt."""

    success: bool
    order_id: str | None
    token_id: str
    direction: str
    price: float
    size_usdc: float
    shares: float
    paper: bool
    error: str | None = None
    raw_response: dict | None = None


def _is_retryable_error(exc: Exception) -> bool:
    """Return True for transient network/rate-limit errors worth retrying."""
    transient = ["timeout", "connection", "rate limit", "503", "502", "504"]
    msg = str(exc).lower()
    return any(kw in msg for kw in transient)


@retry(
    retry=retry_if_exception_type(Exception),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    reraise=True,
)
def _submit_order(clob_client: Any, order_args: dict) -> dict:
    """Submit an order to CLOB with exponential backoff retry."""
    return clob_client.post_order(order_args)


def place_market_order(
    clob_client: Any,
    token_id: str,
    direction: str,         # "YES" or "NO"
    size_usdc: float,
    price: float,
    paper: bool | None = None,
) -> OrderResult:
    """
    Place a market order via the CLOB.

    In paper mode (PAPER_TRADING=true), logs the order without submitting.

    Args:
        clob_client: Initialized ClobClient from wallet_manager
        token_id:    Polymarket YES token ID
        direction:   "YES" to buy YES tokens, "NO" to buy NO tokens
        size_usdc:   USDC amount to spend
        price:       Expected price per share (for slippage estimation)
        paper:       Override paper mode (defaults to settings.paper_trading)

    Returns:
        OrderResult with success status and order ID (or paper ID)
    """
    if paper is None:
        paper = settings.paper_trading

    shares = size_usdc / price if price > 0 else 0.0

    if paper:
        fake_id = f"PAPER-{int(time.time())}-{token_id[:8]}"
        logger.info(
            "PAPER ORDER: %s %s | price=%.4f | size=%.2f USDC | shares=%.2f | id=%s",
            direction, token_id[:12], price, size_usdc, shares, fake_id,
        )
        return OrderResult(
            success=True,
            order_id=fake_id,
            token_id=token_id,
            direction=direction,
            price=price,
            size_usdc=size_usdc,
            shares=shares,
            paper=True,
        )

    # Live order
    try:
        from py_clob_client.clob_types import OrderArgs, OrderType, Side

        side = Side.BUY
        # For NO: we buy the complementary token (1 - token_id's pair)
        # In Polymarket, each market has two tokens: YES and NO
        # We always pass the YES token_id; for NO trades, price = 1 - yes_price

        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=shares,
            side=side,
        )

        response = _submit_order(clob_client, order_args)

        order_id = response.get("orderID") or response.get("id") or response.get("order_id")

        logger.info(
            "LIVE ORDER PLACED: %s %s | price=%.4f | size=%.2f USDC | id=%s",
            direction, token_id[:12], price, size_usdc, order_id,
        )

        return OrderResult(
            success=True,
            order_id=str(order_id),
            token_id=token_id,
            direction=direction,
            price=price,
            size_usdc=size_usdc,
            shares=shares,
            paper=False,
            raw_response=response,
        )

    except Exception as exc:
        logger.error(
            "Order placement FAILED: %s %s | price=%.4f | error=%s",
            direction, token_id[:12], price, exc,
        )
        return OrderResult(
            success=False,
            order_id=None,
            token_id=token_id,
            direction=direction,
            price=price,
            size_usdc=size_usdc,
            shares=shares,
            paper=False,
            error=str(exc),
        )


def cancel_order(clob_client: Any, order_id: str, paper: bool | None = None) -> bool:
    """
    Cancel an open order by ID.

    Returns:
        True if cancellation succeeded (or paper mode), False otherwise.
    """
    if paper is None:
        paper = settings.paper_trading

    if paper:
        logger.info("PAPER CANCEL: order_id=%s", order_id)
        return True

    try:
        clob_client.cancel(order_id)
        logger.info("Order cancelled: %s", order_id)
        return True
    except Exception as exc:
        logger.error("Cancel failed for order %s: %s", order_id, exc)
        return False


def get_open_orders(clob_client: Any) -> list[dict]:
    """Fetch all currently open orders from the CLOB."""
    try:
        orders = clob_client.get_orders()
        return orders if orders else []
    except Exception as exc:
        logger.error("Failed to fetch open orders: %s", exc)
        return []
