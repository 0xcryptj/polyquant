"""
Calibration model: Logistic Regression trained on BTC features.

Outputs P(YES | features) — a calibrated probability that BTC will be
higher in 5 minutes. This is compared against the Polymarket YES token
price to identify mispricings.

Design notes:
- LogisticRegressionCV with Platt scaling for probability calibration
- StandardScaler fit on training data only (prevent leakage)
- Brier score tracked for ongoing calibration quality monitoring
- Model serialized with joblib for persistence between runs
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sklearn.calibration import CalibratedClassifierCV, calibration_curve
from sklearn.linear_model import LogisticRegressionCV
from sklearn.metrics import brier_score_loss, log_loss, roc_auc_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

logger = logging.getLogger(__name__)

MODEL_DIR = Path("models/saved")
MODEL_DIR.mkdir(parents=True, exist_ok=True)


def build_pipeline() -> Pipeline:
    """
    Build the sklearn Pipeline: StandardScaler → LogisticRegressionCV.

    Wrapped in CalibratedClassifierCV for reliable probability outputs.
    """
    base = LogisticRegressionCV(
        Cs=10,
        cv=5,
        max_iter=1000,
        solver="lbfgs",
        n_jobs=-1,
        random_state=42,
        class_weight="balanced",
    )
    calibrated = CalibratedClassifierCV(base, cv=5, method="sigmoid")
    pipeline = Pipeline([
        ("scaler", StandardScaler()),
        ("model", calibrated),
    ])
    return pipeline


def train(
    X_train: pd.DataFrame,
    y_train: pd.Series,
    X_val: pd.DataFrame | None = None,
    y_val: pd.Series | None = None,
) -> tuple[Pipeline, dict[str, float]]:
    """
    Fit the calibration pipeline on training data.

    Args:
        X_train: Feature matrix (no NaN)
        y_train: Binary labels (0 or 1)
        X_val:   Optional validation set for metric computation
        y_val:   Optional validation labels

    Returns:
        (fitted_pipeline, metrics_dict)
        metrics_dict contains train/val Brier score, AUC, log_loss
    """
    if len(X_train) < 50:
        raise ValueError(f"Too few training samples ({len(X_train)}). Need at least 50.")

    pipeline = build_pipeline()
    pipeline.fit(X_train, y_train)

    metrics: dict[str, float] = {}

    # Training metrics
    train_proba = pipeline.predict_proba(X_train)[:, 1]
    metrics["train_brier"] = float(brier_score_loss(y_train, train_proba))
    metrics["train_logloss"] = float(log_loss(y_train, train_proba))
    metrics["train_auc"] = float(roc_auc_score(y_train, train_proba))
    metrics["n_train"] = len(X_train)

    # Validation metrics (if provided)
    if X_val is not None and y_val is not None:
        val_proba = pipeline.predict_proba(X_val)[:, 1]
        metrics["val_brier"] = float(brier_score_loss(y_val, val_proba))
        metrics["val_logloss"] = float(log_loss(y_val, val_proba))
        metrics["val_auc"] = float(roc_auc_score(y_val, val_proba))
        metrics["n_val"] = len(X_val)

    logger.info(
        "Model trained: n=%d | train_brier=%.4f | train_auc=%.4f%s",
        len(X_train),
        metrics["train_brier"],
        metrics["train_auc"],
        f" | val_brier={metrics['val_brier']:.4f}" if "val_brier" in metrics else "",
    )
    return pipeline, metrics


def predict_proba(pipeline: Pipeline, X: pd.DataFrame) -> np.ndarray:
    """Return P(YES) for each row in X."""
    return pipeline.predict_proba(X)[:, 1]


def evaluate_calibration(
    y_true: pd.Series | np.ndarray,
    y_prob: np.ndarray,
    n_bins: int = 10,
) -> dict[str, Any]:
    """
    Full calibration evaluation.

    Returns:
        dict with: brier_score, log_loss, auc, calibration_curve (fraction_pos, mean_pred)
    """
    fraction_of_positives, mean_predicted_value = calibration_curve(
        y_true, y_prob, n_bins=n_bins, strategy="uniform"
    )
    return {
        "brier_score": float(brier_score_loss(y_true, y_prob)),
        "log_loss": float(log_loss(y_true, y_prob)),
        "auc": float(roc_auc_score(y_true, y_prob)),
        "calibration_fraction_pos": fraction_of_positives.tolist(),
        "calibration_mean_pred": mean_predicted_value.tolist(),
        "n_samples": len(y_true),
    }


def save_model(pipeline: Pipeline, name: str = "calibration_model") -> Path:
    """Serialize the fitted pipeline to disk."""
    import joblib
    path = MODEL_DIR / f"{name}.joblib"
    joblib.dump(pipeline, path)
    logger.info("Model saved to %s", path)
    return path


def load_model(name: str = "calibration_model") -> Pipeline:
    """Load a previously saved pipeline."""
    import joblib
    path = MODEL_DIR / f"{name}.joblib"
    if not path.exists():
        raise FileNotFoundError(
            f"No saved model at {path}. Run the backtest/training pipeline first."
        )
    pipeline = joblib.load(path)
    logger.info("Model loaded from %s", path)
    return pipeline
