"""
Expected Value (EV) filter with Polymarket fee correction.

Polymarket charges a 2% taker fee on winning positions.
Net EV must be positive after fee before placing a trade.

EV formula (buying YES at price p):
    P(YES)  = model probability
    p       = market price (YES token cost)
    fee     = 0.02 (2% of payout on win)

    EV = P(YES) * (1 - fee) - p        [for a YES trade]
    EV = (1 - P(YES)) * (1 - fee) - (1 - p)  [for a NO trade]

We only trade when EV > MIN_EDGE_THRESHOLD (configurable).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from config.settings import settings

logger = logging.getLogger(__name__)

FEE = settings.POLYMARKET_FEE  # 0.02


@dataclass
class TradeSignal:
    """Output from EV filter for a single market snapshot."""

    token_id: str
    model_prob: float     # P(YES) from calibration model
    market_price: float   # YES token price (bid or ask)
    direction: str        # "YES" or "NO"
    raw_ev: float         # EV before fee
    net_ev: float         # EV after fee
    edge: float           # net_ev (>0 means trade)
    should_trade: bool
    reason: str           # human-readable explanation


def compute_ev(
    model_prob: float,
    yes_price: float,
    no_price: float | None = None,
    fee: float = FEE,
) -> tuple[float, float]:
    """
    Compute net EV for both YES and NO sides.

    Args:
        model_prob: P(YES) from model (0 to 1)
        yes_price:  Market price of YES token (typically the ask for buying)
        no_price:   Market price of NO token. If None, inferred as 1 - yes_price.
        fee:        Polymarket taker fee on winnings.

    Returns:
        (yes_ev, no_ev) — net expected value for each side
    """
    if not 0.0 < model_prob < 1.0:
        raise ValueError(f"model_prob must be in (0, 1), got {model_prob}")
    if not 0.0 < yes_price < 1.0:
        raise ValueError(f"yes_price must be in (0, 1), got {yes_price}")

    if no_price is None:
        no_price = 1.0 - yes_price

    # EV of buying YES: win (1 - fee) if BTC goes up, lose yes_price if not
    yes_ev = model_prob * (1.0 - fee) - yes_price

    # EV of buying NO: win (1 - fee) if BTC goes down, lose no_price if not
    no_ev = (1.0 - model_prob) * (1.0 - fee) - no_price

    return yes_ev, no_ev


def evaluate_trade(
    token_id: str,
    model_prob: float,
    best_ask: float,      # price to buy YES (ask side)
    best_bid: float,      # price to sell YES (bid side); used for NO trade cost
    spread: float | None = None,
    min_edge: float | None = None,
    max_spread: float | None = None,
) -> TradeSignal:
    """
    Full trade evaluation: EV, edge, spread, and regime checks.

    Args:
        token_id:   Polymarket YES token ID
        model_prob: P(YES) from calibration model
        best_ask:   Current ask price for YES (we pay this to buy YES)
        best_bid:   Current bid price for YES (1 - bid = price to buy NO)
        spread:     bid-ask spread (computed if not provided)
        min_edge:   Minimum net EV to trade (defaults to settings)
        max_spread: Maximum spread to tolerate (defaults to settings)

    Returns:
        TradeSignal with all details.
    """
    min_edge = min_edge if min_edge is not None else settings.min_edge_threshold
    max_spread = max_spread if max_spread is not None else settings.max_spread

    if spread is None:
        spread = best_ask - best_bid

    # Spread check
    if spread > max_spread:
        return TradeSignal(
            token_id=token_id,
            model_prob=model_prob,
            market_price=best_ask,
            direction="NONE",
            raw_ev=0.0,
            net_ev=0.0,
            edge=0.0,
            should_trade=False,
            reason=f"Spread {spread:.4f} > max_spread {max_spread:.4f}",
        )

    # Cost to buy NO = 1 - best_bid (worst case: take the bid side as NO ask)
    no_price = 1.0 - best_bid

    yes_ev, no_ev = compute_ev(model_prob, best_ask, no_price)

    # Best direction
    if yes_ev >= no_ev and yes_ev > min_edge:
        direction = "YES"
        edge = yes_ev
        market_price = best_ask
    elif no_ev > yes_ev and no_ev > min_edge:
        direction = "NO"
        edge = no_ev
        market_price = no_price
    else:
        best_ev = max(yes_ev, no_ev)
        return TradeSignal(
            token_id=token_id,
            model_prob=model_prob,
            market_price=best_ask,
            direction="NONE",
            raw_ev=best_ev + FEE,
            net_ev=best_ev,
            edge=best_ev,
            should_trade=False,
            reason=f"Max EV {best_ev:.4f} < min_edge {min_edge:.4f}",
        )

    logger.debug(
        "EV signal: %s | P(YES)=%.3f | direction=%s | edge=%.4f | spread=%.4f",
        token_id[:12], model_prob, direction, edge, spread,
    )

    return TradeSignal(
        token_id=token_id,
        model_prob=model_prob,
        market_price=market_price,
        direction=direction,
        raw_ev=edge + FEE,
        net_ev=edge,
        edge=edge,
        should_trade=True,
        reason=f"Edge {edge:.4f} >= min_edge {min_edge:.4f}",
    )
