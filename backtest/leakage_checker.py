"""
Pre-flight anti-data-leakage checklist.

Data leakage is the #1 cause of backtests that look great but fail live.
This module runs a series of checks before any backtest begins.

Common leakage vectors in this system:
1. Label uses future data (close[t+5]) — must not be in feature set
2. Features computed using data beyond bar t
3. GARCH sigma_t computed with full-sample normalization
4. Model fitted on test data (train/test confusion)
5. Market price alignment issues (survivorship bias, look-ahead fills)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class LeakageCheckResult:
    """Result of a single leakage check."""

    name: str
    passed: bool
    details: str


class LeakageError(ValueError):
    """Raised when a critical leakage check fails."""


def check_no_leakage(
    features: pd.DataFrame,
    labels: pd.Series,
    raise_on_failure: bool = True,
) -> list[LeakageCheckResult]:
    """
    Run all leakage checks on a (features, labels) pair.

    Args:
        features:         Feature matrix (DatetimeIndex)
        labels:           Binary labels aligned with features
        raise_on_failure: If True, raise LeakageError on any failure.

    Returns:
        List of LeakageCheckResult for each check.
    """
    results: list[LeakageCheckResult] = []

    results.append(_check_index_alignment(features, labels))
    results.append(_check_no_future_features(features))
    results.append(_check_no_constant_features(features))
    results.append(_check_no_nan_in_features(features))
    results.append(_check_label_distribution(labels))
    results.append(_check_temporal_ordering(features))

    failures = [r for r in results if not r.passed]

    if failures:
        msg_lines = [f"DATA LEAKAGE DETECTED — {len(failures)} check(s) failed:"]
        for f in failures:
            msg_lines.append(f"  ✗ {f.name}: {f.details}")
        msg = "\n".join(msg_lines)
        logger.error(msg)
        if raise_on_failure:
            raise LeakageError(msg)
    else:
        logger.info("All %d leakage checks passed.", len(results))

    return results


def _check_index_alignment(features: pd.DataFrame, labels: pd.Series) -> LeakageCheckResult:
    """Features and labels must have identical indices."""
    name = "index_alignment"
    if not features.index.equals(labels.index):
        n_mismatch = len(features.index.symmetric_difference(labels.index))
        return LeakageCheckResult(
            name=name, passed=False,
            details=f"Feature/label index mismatch: {n_mismatch} differing timestamps."
        )
    return LeakageCheckResult(name=name, passed=True, details=f"Indices match ({len(features)} rows)")


def _check_no_future_features(features: pd.DataFrame) -> LeakageCheckResult:
    """
    Check for obviously future-leaking column names.

    This is a heuristic — a human should also review feature definitions.
    """
    name = "no_future_features"
    suspicious = [
        col for col in features.columns
        if any(kw in col.lower() for kw in ["future", "forward", "next", "lead", "fwd", "t+"])
    ]
    if suspicious:
        return LeakageCheckResult(
            name=name, passed=False,
            details=f"Suspicious column names suggesting future data: {suspicious}"
        )
    return LeakageCheckResult(name=name, passed=True, details="No obviously future-leaking column names")


def _check_no_constant_features(features: pd.DataFrame) -> LeakageCheckResult:
    """Features that are constant across all rows are useless and may indicate bugs."""
    name = "no_constant_features"
    constant_cols = [col for col in features.columns if features[col].nunique() <= 1]
    if constant_cols:
        return LeakageCheckResult(
            name=name, passed=False,
            details=f"Constant (zero-variance) features detected: {constant_cols}"
        )
    return LeakageCheckResult(name=name, passed=True, details="All features have variance > 0")


def _check_no_nan_in_features(features: pd.DataFrame) -> LeakageCheckResult:
    """No NaN should remain after align_features_labels is called."""
    name = "no_nan_features"
    nan_cols = features.columns[features.isna().any()].tolist()
    if nan_cols:
        n_nan = int(features.isna().sum().sum())
        return LeakageCheckResult(
            name=name, passed=False,
            details=f"{n_nan} NaN values in columns: {nan_cols}"
        )
    return LeakageCheckResult(name=name, passed=True, details="No NaN values in features")


def _check_label_distribution(labels: pd.Series) -> LeakageCheckResult:
    """Labels should be reasonably balanced and near 50% (BTC up/down markets)."""
    name = "label_distribution"
    pos_rate = float(labels.mean())

    if pos_rate < 0.30 or pos_rate > 0.70:
        return LeakageCheckResult(
            name=name, passed=False,
            details=(
                f"Label imbalance: {pos_rate:.1%} YES outcomes. "
                "Expect ~50% for BTC 5-min markets. "
                "Check label construction for bugs."
            )
        )
    return LeakageCheckResult(
        name=name, passed=True,
        details=f"Label balance: {pos_rate:.1%} YES ({len(labels)} total)"
    )


def _check_temporal_ordering(features: pd.DataFrame) -> LeakageCheckResult:
    """DatetimeIndex should be strictly ascending (no out-of-order data)."""
    name = "temporal_ordering"
    if not features.index.is_monotonic_increasing:
        n_inversions = int((features.index[1:] < features.index[:-1]).sum())
        return LeakageCheckResult(
            name=name, passed=False,
            details=f"Index is not monotonically increasing: {n_inversions} inversions detected."
        )
    return LeakageCheckResult(name=name, passed=True, details="Index is strictly ascending")


def check_train_test_separation(
    train_end: pd.Timestamp,
    test_start: pd.Timestamp,
    min_gap_minutes: int = 5,
) -> LeakageCheckResult:
    """
    Verify train set ends before test set begins with a sufficient gap.

    The gap prevents the model seeing data that 'bleeds' into both windows
    due to rolling feature computations.
    """
    name = "train_test_gap"
    gap = (test_start - train_end).total_seconds() / 60
    if gap < min_gap_minutes:
        return LeakageCheckResult(
            name=name, passed=False,
            details=(
                f"Train-test gap is only {gap:.1f} minutes (need >= {min_gap_minutes}). "
                "Rolling features from train window can bleed into test."
            )
        )
    return LeakageCheckResult(
        name=name, passed=True,
        details=f"Train-test gap: {gap:.1f} minutes"
    )


def check_scaler_fit_on_train_only(
    pipeline,
    X_train: pd.DataFrame,
    X_test: pd.DataFrame,
) -> LeakageCheckResult:
    """
    Verify scaler was fit only on training data by checking that test data
    contains values outside the training data range (if any exist in reality).
    This is a sanity check — the real enforcement is in the Pipeline code.
    """
    name = "scaler_train_only"
    try:
        scaler = pipeline.named_steps.get("scaler")
        if scaler is None:
            return LeakageCheckResult(name=name, passed=True, details="No scaler in pipeline")

        train_max = X_train.max()
        test_max = X_test.max()

        # If test max exactly equals train max for ALL features, scaler was likely refit on test
        if (test_max == train_max).all():
            return LeakageCheckResult(
                name=name, passed=False,
                details="Test set max equals train set max for all features — possible scaler leakage."
            )
        return LeakageCheckResult(name=name, passed=True, details="Scaler appears fit on train only")
    except Exception as exc:
        return LeakageCheckResult(name=name, passed=False, details=f"Could not verify: {exc}")
