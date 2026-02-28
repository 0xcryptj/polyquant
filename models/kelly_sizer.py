"""
Fractional Kelly position sizer with Polymarket fee correction.

Kelly Criterion: f* = (p * b - q) / b
  where:
    p = probability of winning
    b = net odds (payout per unit risked)
    q = 1 - p

For Polymarket binary markets (binary outcome, price in [0,1]):
    Buying YES at price `c` (cost per share):
        Win:  payout = 1, net gain = (1 - fee) - c
        Lose: lose c
    So b = (1 - fee - c) / c
       f* = (p * b - (1-p)) / b

We then apply kelly_fraction multiplier (e.g. 0.25 for quarter-Kelly)
and clamp to [0, max_position_usdc].
"""

from __future__ import annotations

import logging

from config.settings import settings

logger = logging.getLogger(__name__)

FEE = settings.POLYMARKET_FEE


def kelly_fraction_binary(
    prob_win: float,
    cost_per_share: float,
    fee: float = FEE,
) -> float:
    """
    Compute the full Kelly fraction for a Polymarket binary position.

    Args:
        prob_win:       Model probability of winning (0 < p < 1)
        cost_per_share: Price per share (0 < c < 1, e.g. 0.52 for YES at 52¢)
        fee:            Polymarket taker fee on winnings (default 2%)

    Returns:
        Full Kelly fraction as a proportion of bankroll [0, 1].
        Returns 0.0 if EV is not positive.

    Note:
        This is the FULL Kelly. Always multiply by settings.kelly_fraction
        (fractional Kelly) before using. Full Kelly maximizes log-wealth
        but has extreme variance — use at most 0.25x in practice.
    """
    if not (0.0 < prob_win < 1.0):
        raise ValueError(f"prob_win must be in (0, 1), got {prob_win}")
    if not (0.0 < cost_per_share < 1.0):
        raise ValueError(f"cost_per_share must be in (0, 1), got {cost_per_share}")

    net_win = (1.0 - fee) - cost_per_share  # net gain per share on a win
    net_loss = cost_per_share               # loss per share on a loss

    if net_win <= 0:
        return 0.0  # cost >= payout after fee → never trade

    # Standard Kelly: f* = (p * b - q) / b where b = net_win / net_loss
    b = net_win / net_loss
    q = 1.0 - prob_win
    full_kelly = (prob_win * b - q) / b

    return max(0.0, full_kelly)


def size_position(
    prob_win: float,
    cost_per_share: float,
    bankroll_usdc: float,
    kelly_multiplier: float | None = None,
    max_usdc: float | None = None,
    fee: float = FEE,
) -> float:
    """
    Compute position size in USDC.

    Args:
        prob_win:         Model probability of winning
        cost_per_share:   Price per share (YES or NO token price)
        bankroll_usdc:    Current available USDC balance
        kelly_multiplier: Fractional Kelly (defaults to settings.kelly_fraction)
        max_usdc:         Hard cap per trade (defaults to settings.max_position_usdc)
        fee:              Polymarket fee

    Returns:
        Position size in USDC to bet. May be 0.0 if no edge.
    """
    kelly_multiplier = kelly_multiplier if kelly_multiplier is not None else settings.kelly_fraction
    max_usdc = max_usdc if max_usdc is not None else settings.max_position_usdc

    if bankroll_usdc <= 0:
        logger.warning("Bankroll is 0 or negative: %.2f USDC", bankroll_usdc)
        return 0.0

    full_kelly = kelly_fraction_binary(prob_win, cost_per_share, fee)

    if full_kelly <= 0.0:
        return 0.0

    # Apply fractional Kelly
    frac_kelly = full_kelly * kelly_multiplier

    # Position size in USDC
    position_usdc = frac_kelly * bankroll_usdc

    # Apply hard cap
    final_size = min(position_usdc, max_usdc)

    logger.debug(
        "Kelly sizing: P=%.3f c=%.3f bankroll=%.1f | "
        "full_k=%.4f frac_k=%.4f raw_usdc=%.2f final_usdc=%.2f",
        prob_win, cost_per_share, bankroll_usdc,
        full_kelly, frac_kelly, position_usdc, final_size,
    )

    return final_size


def shares_from_usdc(usdc_amount: float, cost_per_share: float) -> float:
    """Convert USDC amount to number of shares at a given price."""
    if cost_per_share <= 0:
        raise ValueError(f"cost_per_share must be positive, got {cost_per_share}")
    return usdc_amount / cost_per_share


def expected_profit(
    prob_win: float,
    shares: float,
    cost_per_share: float,
    fee: float = FEE,
) -> float:
    """
    Expected profit in USDC for a given position.

    Args:
        prob_win:       Probability of winning
        shares:         Number of shares purchased
        cost_per_share: Price paid per share
        fee:            Polymarket fee

    Returns:
        Expected profit (positive = profitable trade)
    """
    win_profit = shares * (1.0 - fee) - shares * cost_per_share
    loss = -shares * cost_per_share
    return prob_win * win_profit + (1.0 - prob_win) * loss
