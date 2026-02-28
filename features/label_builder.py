"""
Ground truth label construction for BTC 5-minute up/down markets.

The label is the actual market outcome:
    1 = BTC price was HIGHER at end of 5-min window than start (YES resolves)
    0 = BTC price was LOWER or equal (NO resolves)

For live calibration, labels come from resolved Polymarket markets.
For training, they are derived from the BTC close prices.
"""

from __future__ import annotations

import logging

import pandas as pd

logger = logging.getLogger(__name__)


def build_labels_from_ohlcv(
    ohlcv: pd.DataFrame,
    horizon_bars: int = 5,
) -> pd.Series:
    """
    Build binary labels from BTC close prices.

    Label at bar t = 1 if close[t + horizon_bars] > close[t], else 0.
    This simulates the "will BTC be higher in 5 minutes?" outcome.

    Args:
        ohlcv:         DataFrame with 'close' column and DatetimeIndex (1m bars)
        horizon_bars:  Number of bars forward to look (5 for a 5-min market)

    Returns:
        Boolean Series (True=YES, False=NO), NaN after last horizon_bars bars.
        Must be aligned with the feature matrix row-for-row.

    IMPORTANT: The label at bar t uses future data (close at t+5).
               The feature matrix must only use data from bars <= t.
               The walk-forward engine enforces this; the leakage_checker verifies it.
    """
    if "close" not in ohlcv.columns:
        raise ValueError("ohlcv DataFrame must have a 'close' column")

    future_close = ohlcv["close"].shift(-horizon_bars)
    labels = (future_close > ohlcv["close"]).astype(float)

    # Mark the last horizon_bars rows as NaN (no future data)
    labels.iloc[-horizon_bars:] = float("nan")

    valid_count = labels.notna().sum()
    pos_rate = labels.mean()
    logger.info(
        "Labels built: %d valid bars, %.1f%% YES outcomes (horizon=%d bars)",
        valid_count, 100 * pos_rate, horizon_bars,
    )
    return labels


def build_labels_from_resolved_markets(
    market_df: pd.DataFrame,
    outcome_col: str = "resolved_outcome",
) -> pd.Series:
    """
    Build labels from actual resolved Polymarket outcomes.

    Args:
        market_df:    DataFrame with market data, indexed by timestamp.
                      Must contain outcome_col with values 'YES'/'NO' or 1/0.
        outcome_col:  Column name for the resolved outcome.

    Returns:
        Integer Series (1=YES, 0=NO).
    """
    if outcome_col not in market_df.columns:
        raise ValueError(f"Column '{outcome_col}' not found in market_df. Available: {list(market_df.columns)}")

    raw = market_df[outcome_col]

    # Normalize to 0/1 regardless of input format
    if raw.dtype == object:
        labels = raw.map({"YES": 1, "NO": 0, "yes": 1, "no": 0}).astype(float)
    else:
        labels = raw.astype(float)

    n_null = labels.isna().sum()
    if n_null > 0:
        logger.warning("Label column has %d NaN values — markets not yet resolved?", n_null)

    pos_rate = labels.mean()
    logger.info(
        "Resolved labels: %d rows, %.1f%% YES, %d unresolved",
        len(labels), 100 * pos_rate, n_null,
    )
    return labels


def align_features_labels(
    features: pd.DataFrame,
    labels: pd.Series,
) -> tuple[pd.DataFrame, pd.Series]:
    """
    Align feature matrix and label series, dropping NaN rows from both.

    Returns:
        (X, y) with matching indices and no NaN values.
    """
    combined = features.copy()
    combined["__label__"] = labels

    before = len(combined)
    combined = combined.dropna()
    after = len(combined)

    if before > after:
        logger.info("Dropped %d rows with NaN features or labels (%d → %d)", before - after, before, after)

    X = combined.drop(columns=["__label__"])
    y = combined["__label__"].astype(int)
    return X, y
