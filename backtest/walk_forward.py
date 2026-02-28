"""
Walk-forward backtesting engine.

Methodology:
1. Split historical data into expanding train windows + fixed test windows
2. For each fold: fit model on train, evaluate on test (no look-ahead)
3. Simulate trade execution with Kelly sizing and EV filter
4. Aggregate performance metrics across all folds

This is the gold standard for strategy validation — avoids the
overfitting endemic to simple train/test splits.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Iterator

import numpy as np
import pandas as pd

from backtest.metrics import BacktestMetrics, compute_metrics
from backtest.leakage_checker import check_no_leakage
from features.label_builder import align_features_labels
from models.calibration_model import train, predict_proba
from models.ev_filter import evaluate_trade
from models.kelly_sizer import size_position
from config.settings import settings

logger = logging.getLogger(__name__)


@dataclass
class FoldResult:
    """Results for a single walk-forward fold."""

    fold_id: int
    train_start: pd.Timestamp
    train_end: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp
    train_metrics: dict
    test_metrics: BacktestMetrics
    trades: list[dict] = field(default_factory=list)
    n_train: int = 0
    n_test: int = 0
    n_trades: int = 0


@dataclass
class WalkForwardResult:
    """Aggregated results across all folds."""

    folds: list[FoldResult]
    all_trades: pd.DataFrame
    aggregate_metrics: dict
    equity_curve: pd.Series


def generate_folds(
    index: pd.DatetimeIndex,
    min_train_bars: int,
    test_bars: int,
    step_bars: int | None = None,
) -> Iterator[tuple[pd.DatetimeIndex, pd.DatetimeIndex]]:
    """
    Generate (train_index, test_index) pairs for walk-forward splits.

    Uses an EXPANDING train window (grows with each fold).

    Args:
        index:           Full data index (sorted ascending)
        min_train_bars:  Minimum bars required before first test fold
        test_bars:       Bars per test fold
        step_bars:       How many bars to advance per fold (defaults to test_bars)
    """
    if step_bars is None:
        step_bars = test_bars

    n = len(index)
    start = min_train_bars

    while start + test_bars <= n:
        train_idx = index[:start]
        test_idx = index[start : start + test_bars]
        yield train_idx, test_idx
        start += step_bars


def run_walk_forward(
    features: pd.DataFrame,
    labels: pd.Series,
    market_prices_df: pd.DataFrame,    # columns: token_id, best_ask, best_bid, spread
    initial_bankroll: float = 1000.0,
    min_train_bars: int = 5040,        # 3.5 days of 1m bars
    test_bars: int = 1440,             # 1 day test windows
    step_bars: int | None = None,
    paper_mode: bool = True,
) -> WalkForwardResult:
    """
    Execute the full walk-forward backtest.

    Args:
        features:          Feature matrix (DatetimeIndex, no NaN)
        labels:            Binary outcome labels aligned with features
        market_prices_df:  Polymarket order book data aligned to features index.
                           Required columns: token_id, best_ask, best_bid, spread
        initial_bankroll:  Starting USDC balance
        min_train_bars:    Min observations before first test
        test_bars:         Bars per test window
        step_bars:         Step between test windows (default = test_bars)
        paper_mode:        If True, use paper prices (no slippage simulation)

    Returns:
        WalkForwardResult with per-fold and aggregate stats
    """
    # Pre-flight leakage check
    check_no_leakage(features, labels)

    X, y = align_features_labels(features, labels)
    common_idx = X.index.intersection(market_prices_df.index)
    X = X.loc[common_idx]
    y = y.loc[common_idx]
    mkt = market_prices_df.loc[common_idx]

    logger.info(
        "Walk-forward: %d total bars | train_min=%d | test=%d | bankroll=%.2f",
        len(X), min_train_bars, test_bars, initial_bankroll,
    )

    folds: list[FoldResult] = []
    all_trade_rows: list[dict] = []
    bankroll = initial_bankroll
    fold_id = 0

    for train_idx, test_idx in generate_folds(X.index, min_train_bars, test_bars, step_bars):
        X_train, y_train = X.loc[train_idx], y.loc[train_idx]
        X_test, y_test = X.loc[test_idx], y.loc[test_idx]
        mkt_test = mkt.loc[test_idx]

        if len(X_train) < 50 or len(X_test) < 10:
            logger.warning("Fold %d: insufficient data (train=%d, test=%d), skipping", fold_id, len(X_train), len(X_test))
            fold_id += 1
            continue

        # Train model on training window
        pipeline, train_metrics = train(X_train, y_train, X_test, y_test)

        # Generate predictions on test window
        test_proba = predict_proba(pipeline, X_test)

        # Simulate trades on test window
        fold_trades: list[dict] = []
        start_bankroll = bankroll

        for i, (ts, prob) in enumerate(zip(X_test.index, test_proba)):
            if ts not in mkt_test.index:
                continue

            row = mkt_test.loc[ts]
            token_id = str(row.get("token_id", "unknown"))
            best_ask = float(row.get("best_ask", 0.5))
            best_bid = float(row.get("best_bid", 0.5))
            spread = float(row.get("spread", best_ask - best_bid))

            signal = evaluate_trade(
                token_id=token_id,
                model_prob=prob,
                best_ask=best_ask,
                best_bid=best_bid,
                spread=spread,
            )

            if not signal.should_trade:
                continue

            # Size position
            cost = signal.market_price
            size_usdc = size_position(
                prob_win=prob if signal.direction == "YES" else 1 - prob,
                cost_per_share=cost,
                bankroll_usdc=bankroll,
            )

            if size_usdc < 1.0:
                continue

            shares = size_usdc / cost

            # Resolve outcome
            actual_outcome = int(y_test.iloc[i])
            won = (signal.direction == "YES" and actual_outcome == 1) or \
                  (signal.direction == "NO" and actual_outcome == 0)

            pnl = shares * (1.0 - FEE) - size_usdc if won else -size_usdc
            bankroll += pnl

            trade = {
                "timestamp": ts,
                "fold": fold_id,
                "token_id": token_id,
                "direction": signal.direction,
                "model_prob": prob,
                "market_price": cost,
                "edge": signal.edge,
                "size_usdc": size_usdc,
                "shares": shares,
                "outcome": actual_outcome,
                "won": won,
                "pnl": pnl,
                "bankroll": bankroll,
            }
            fold_trades.append(trade)
            all_trade_rows.append(trade)

        trades_df = pd.DataFrame(fold_trades) if fold_trades else pd.DataFrame()
        test_metrics = compute_metrics(
            y_true=y_test.values,
            y_prob=test_proba,
            trades=trades_df,
            starting_bankroll=start_bankroll,
        )

        fold_result = FoldResult(
            fold_id=fold_id,
            train_start=train_idx[0],
            train_end=train_idx[-1],
            test_start=test_idx[0],
            test_end=test_idx[-1],
            train_metrics=train_metrics,
            test_metrics=test_metrics,
            trades=fold_trades,
            n_train=len(X_train),
            n_test=len(X_test),
            n_trades=len(fold_trades),
        )
        folds.append(fold_result)

        logger.info(
            "Fold %d: trades=%d | brier=%.4f | pnl=%.2f | bankroll=%.2f",
            fold_id, len(fold_trades),
            test_metrics.brier_score if test_metrics.brier_score else 0,
            test_metrics.total_pnl,
            bankroll,
        )
        fold_id += 1

    # Aggregate
    all_trades_df = pd.DataFrame(all_trade_rows) if all_trade_rows else pd.DataFrame()
    equity = all_trades_df["bankroll"] if not all_trades_df.empty else pd.Series([initial_bankroll])

    aggregate = _aggregate_fold_metrics(folds, initial_bankroll, bankroll)

    logger.info(
        "Walk-forward complete: %d folds | %d trades | final_bankroll=%.2f | total_pnl=%.2f (%.1f%%)",
        len(folds), len(all_trade_rows), bankroll,
        bankroll - initial_bankroll, 100 * (bankroll / initial_bankroll - 1),
    )

    return WalkForwardResult(
        folds=folds,
        all_trades=all_trades_df,
        aggregate_metrics=aggregate,
        equity_curve=equity,
    )


def _aggregate_fold_metrics(
    folds: list[FoldResult],
    initial_bankroll: float,
    final_bankroll: float,
) -> dict:
    if not folds:
        return {}

    briors = [f.test_metrics.brier_score for f in folds if f.test_metrics.brier_score is not None]
    sharpes = [f.test_metrics.sharpe_ratio for f in folds if f.test_metrics.sharpe_ratio is not None]
    n_trades = sum(f.n_trades for f in folds)

    return {
        "n_folds": len(folds),
        "n_trades": n_trades,
        "mean_brier": float(np.mean(briors)) if briors else None,
        "mean_sharpe": float(np.mean(sharpes)) if sharpes else None,
        "total_pnl": final_bankroll - initial_bankroll,
        "total_return_pct": 100 * (final_bankroll / initial_bankroll - 1),
        "final_bankroll": final_bankroll,
    }


# Import FEE here to avoid circular imports
FEE = settings.POLYMARKET_FEE
