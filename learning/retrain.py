"""
Retrain the calibration model on closed paper trade outcomes.

Called by Learner when Brier score is poor and enough trade data exists.
Improves P(YES|features) over time as the bot accumulates crypto up/down
trade results.
"""
from __future__ import annotations

import logging
from typing import Any

from learning.trade_to_training import build_xy_from_trades
from paper_trading import persistence as db

logger = logging.getLogger(__name__)


def maybe_retrain(
    min_trades: int = 50,
    min_brier_to_trigger: float = 0.28,
    current_brier: float | None = None,
    n_closed: int | None = None,
) -> dict[str, Any] | None:
    """
    Retrain the calibration model if conditions are met.

    Args:
        min_trades: Minimum closed trades with valid features
        min_brier_to_trigger: Only retrain when Brier > this (model poorly calibrated)
        current_brier: Current Brier score (optional; if None, skips Brier check)
        n_closed: Number of closed trades (optional; if None, computed)

    Returns:
        Metrics dict if retrain ran, None otherwise
    """
    closed = db.get_all_closed_trades()
    n = len(closed)
    if n_closed is not None and n_closed != n:
        n = n_closed
    if n < min_trades:
        return None

    # Optional: only retrain when model is poorly calibrated
    if current_brier is not None and current_brier <= min_brier_to_trigger:
        return None

    xy = build_xy_from_trades(closed)
    if xy is None:
        return None

    X, y = xy

    # Train/val split by time (older = train, newer = val)
    split = int(len(X) * 0.8)
    if split < 30:
        split = len(X)
    X_train, X_val = X.iloc[:split], X.iloc[split:] if split < len(X) else None
    y_train, y_val = y.iloc[:split], y.iloc[split:] if split < len(y) else None

    try:
        from models.calibration_model import train, save_model
        pipeline, metrics = train(
            X_train, y_train,
            X_val=X_val if X_val is not None and len(X_val) >= 10 else None,
            y_val=y_val if y_val is not None and len(y_val) >= 10 else None,
        )
        save_model(pipeline)
        logger.info(
            "Calibration model retrained: n=%d | train_brier=%.4f | val_brier=%s",
            len(X_train),
            metrics.get("train_brier", 0),
            metrics.get("val_brier") if "val_brier" in metrics else "N/A",
        )
        return metrics
    except Exception as exc:
        logger.warning("Retrain failed: %s", exc)
        return None
