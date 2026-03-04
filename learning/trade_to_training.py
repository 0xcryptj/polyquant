"""
Build (X, y) training data from closed paper trades.

Maps trade features (JSON) and outcome (won/lost, direction) to supervised
labels: y=1 means BTC went UP (YES would have won), y=0 means BTC went DOWN.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import numpy as np
import pandas as pd

from features.feature_builder import FEATURE_COLUMNS

logger = logging.getLogger(__name__)

MIN_SAMPLES = 50


def _outcome_to_label(direction: str, status: str) -> int:
    """
    Convert (direction, status) to binary label for P(YES | features).

    YES + won  -> 1 (BTC went up)
    YES + lost -> 0 (BTC went down)
    NO  + won  -> 0 (we bet NO, won => BTC went down)
    NO  + lost -> 1 (we bet NO, lost => BTC went up)
    """
    d = (direction or "YES").upper()
    s = (status or "lost").lower()
    if d == "YES":
        return 1 if s == "won" else 0
    return 0 if s == "won" else 1


def build_xy_from_trades(
    trades: list[dict[str, Any]],
    feature_cols: list[str] | None = None,
) -> tuple[pd.DataFrame, pd.Series] | None:
    """
    Extract (X, y) from closed trades for model retraining.

    Args:
        trades: Closed trade rows with 'features' (JSON), 'direction', 'status'
        feature_cols: Columns to use (default: FEATURE_COLUMNS)

    Returns:
        (X, y) or None if too few valid samples
    """
    if feature_cols is None:
        feature_cols = FEATURE_COLUMNS

    rows_x: list[dict[str, float]] = []
    rows_y: list[int] = []

    for t in trades:
        feats_raw = t.get("features")
        if feats_raw is None:
            continue
        if isinstance(feats_raw, str):
            try:
                feats = json.loads(feats_raw)
            except json.JSONDecodeError:
                continue
        else:
            feats = feats_raw if isinstance(feats_raw, dict) else {}

        direction = t.get("direction", "YES")
        status = t.get("status", "lost")
        if status not in ("won", "lost"):
            continue

        row: dict[str, float] = {}
        missing = 0
        for col in feature_cols:
            v = feats.get(col)
            if v is None or (isinstance(v, float) and np.isnan(v)):
                row[col] = 0.0
                missing += 1
            else:
                try:
                    row[col] = float(v)
                except (TypeError, ValueError):
                    row[col] = 0.0
                    missing += 1

        # Require at least 80% of features present
        if missing > len(feature_cols) * 0.2:
            continue

        rows_x.append(row)
        rows_y.append(_outcome_to_label(direction, status))

    if len(rows_x) < MIN_SAMPLES:
        logger.info(
            "Not enough valid trade samples for retraining: %d < %d",
            len(rows_x), MIN_SAMPLES,
        )
        return None

    X = pd.DataFrame(rows_x)[feature_cols]
    y = pd.Series(rows_y, dtype=np.int64)
    return X, y
