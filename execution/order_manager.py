"""
Order lifecycle manager — place, monitor, and cancel orders.

Also enforces the kill switch (daily drawdown limit).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

from execution.clob_client import OrderResult, place_market_order, cancel_order, get_open_orders
from control.kill_switch import KillSwitch
from config.settings import settings

logger = logging.getLogger(__name__)


@dataclass
class Position:
    """A live or recently closed position."""

    order_id: str
    token_id: str
    direction: str
    entry_price: float
    size_usdc: float
    shares: float
    opened_at: datetime
    closed_at: datetime | None = None
    exit_price: float | None = None
    pnl: float | None = None
    status: str = "open"  # open | filled | cancelled | expired


@dataclass
class OrderManager:
    """
    Stateful order manager for the trading session.

    Tracks open positions, enforces risk limits, and provides
    the interface between the signal generator and the CLOB.
    """

    clob_client: Any
    kill_switch: KillSwitch
    starting_bankroll: float
    current_bankroll: float = field(init=False)
    open_positions: dict[str, Position] = field(default_factory=dict)
    trade_log: list[Position] = field(default_factory=list)
    session_pnl: float = 0.0

    def __post_init__(self):
        self.current_bankroll = self.starting_bankroll

    def can_trade(self) -> tuple[bool, str]:
        """
        Check all preconditions before placing a trade.

        Returns:
            (True, "") if trading is allowed
            (False, reason) if blocked
        """
        # Kill switch
        if self.kill_switch.is_triggered():
            return False, f"Kill switch active: {self.kill_switch.reason}"

        # Max open positions (avoid overexposure)
        max_open = 3
        if len(self.open_positions) >= max_open:
            return False, f"Max open positions reached ({max_open})"

        # Minimum bankroll
        if self.current_bankroll < 5.0:
            return False, f"Bankroll too low: {self.current_bankroll:.2f} USDC"

        return True, ""

    def submit_trade(
        self,
        token_id: str,
        direction: str,
        size_usdc: float,
        price: float,
        model_prob: float,
        edge: float,
    ) -> OrderResult:
        """
        Submit a trade after passing all risk checks.

        Args:
            token_id:    YES token ID
            direction:   "YES" or "NO"
            size_usdc:   Position size in USDC
            price:       Expected execution price
            model_prob:  Model probability (for logging)
            edge:        Expected edge (for logging)

        Returns:
            OrderResult from clob_client
        """
        tradeable, reason = self.can_trade()
        if not tradeable:
            logger.warning("Trade blocked: %s", reason)
            return OrderResult(
                success=False,
                order_id=None,
                token_id=token_id,
                direction=direction,
                price=price,
                size_usdc=size_usdc,
                shares=size_usdc / price if price > 0 else 0,
                paper=settings.paper_trading,
                error=reason,
            )

        result = place_market_order(
            clob_client=self.clob_client,
            token_id=token_id,
            direction=direction,
            size_usdc=size_usdc,
            price=price,
        )

        if result.success and result.order_id:
            position = Position(
                order_id=result.order_id,
                token_id=token_id,
                direction=direction,
                entry_price=price,
                size_usdc=size_usdc,
                shares=result.shares,
                opened_at=datetime.now(timezone.utc),
            )
            self.open_positions[result.order_id] = position
            self.current_bankroll -= size_usdc  # optimistic debit

            logger.info(
                "Position opened: %s %s | prob=%.3f | edge=%.4f | size=%.2f | id=%s",
                direction, token_id[:12], model_prob, edge, size_usdc, result.order_id,
            )

        return result

    def close_position(
        self,
        order_id: str,
        exit_price: float | None = None,
        won: bool | None = None,
    ) -> float:
        """
        Record a position as closed and update PnL.

        Args:
            order_id:   Position identifier
            exit_price: Final settlement price (1.0 for win, 0.0 for loss)
            won:        Override: True=won, False=lost

        Returns:
            Realized PnL in USDC
        """
        pos = self.open_positions.pop(order_id, None)
        if pos is None:
            logger.warning("Tried to close unknown position: %s", order_id)
            return 0.0

        if won is not None:
            exit_price = 1.0 if won else 0.0

        if exit_price is None:
            logger.warning("No exit price for position %s — assuming loss", order_id)
            exit_price = 0.0

        pnl = pos.shares * exit_price * (1 - settings.POLYMARKET_FEE) - pos.size_usdc
        pos.exit_price = exit_price
        pos.pnl = pnl
        pos.status = "filled"
        pos.closed_at = datetime.now(timezone.utc)

        self.current_bankroll += pos.size_usdc + pnl  # restore cost + profit/loss
        self.session_pnl += pnl
        self.trade_log.append(pos)

        # Update kill switch
        self.kill_switch.update(self.current_bankroll)

        logger.info(
            "Position closed: %s | pnl=%+.2f | bankroll=%.2f | session_pnl=%+.2f",
            order_id, pnl, self.current_bankroll, self.session_pnl,
        )
        return pnl

    def cancel_all_open(self) -> int:
        """Cancel all open positions. Returns count of successful cancellations."""
        cancelled = 0
        for order_id in list(self.open_positions.keys()):
            if cancel_order(self.clob_client, order_id):
                self.open_positions.pop(order_id, None)
                cancelled += 1
        logger.info("Cancelled %d open positions", cancelled)
        return cancelled

    def session_summary(self) -> dict:
        """Return a summary of the current trading session."""
        wins = [p for p in self.trade_log if p.pnl and p.pnl > 0]
        losses = [p for p in self.trade_log if p.pnl and p.pnl <= 0]
        return {
            "n_trades": len(self.trade_log),
            "n_open": len(self.open_positions),
            "n_wins": len(wins),
            "n_losses": len(losses),
            "win_rate": len(wins) / len(self.trade_log) if self.trade_log else 0.0,
            "session_pnl": self.session_pnl,
            "current_bankroll": self.current_bankroll,
            "starting_bankroll": self.starting_bankroll,
            "return_pct": 100 * (self.current_bankroll / self.starting_bankroll - 1),
            "kill_switch_triggered": self.kill_switch.is_triggered(),
        }
