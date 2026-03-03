"""
Backtest performance metrics.

Implements:
- Sharpe ratio (annualized, trade-based)
- Sortino ratio
- Maximum drawdown
- Win rate, profit factor
- Brier score (calibration quality)
- Calibration curve data
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd
from sklearn.metrics import brier_score_loss

logger = logging.getLogger(__name__)


@dataclass
class BacktestMetrics:
    """Comprehensive performance metrics for a backtest period."""

    # PnL
    total_pnl: float
    total_return_pct: float
    n_trades: int
    win_rate: float
    profit_factor: float

    # Risk
    sharpe_ratio: float | None
    sortino_ratio: float | None
    max_drawdown_pct: float
    calmar_ratio: float | None

    # Calibration
    brier_score: float | None
    avg_edge: float | None

    # Raw data for plotting
    equity_curve: pd.Series | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_pnl": self.total_pnl,
            "total_return_pct": self.total_return_pct,
            "n_trades": self.n_trades,
            "win_rate": self.win_rate,
            "profit_factor": self.profit_factor,
            "sharpe_ratio": self.sharpe_ratio,
            "sortino_ratio": self.sortino_ratio,
            "max_drawdown_pct": self.max_drawdown_pct,
            "calmar_ratio": self.calmar_ratio,
            "brier_score": self.brier_score,
            "avg_edge": self.avg_edge,
        }

    def __str__(self) -> str:
        lines = [
            "─── Backtest Metrics ───────────────────",
            f"  Trades:        {self.n_trades}",
            f"  Win Rate:      {self.win_rate:.1%}",
            f"  Total PnL:     {self.total_pnl:+.2f} USDC ({self.total_return_pct:+.1f}%)",
            f"  Profit Factor: {self.profit_factor:.2f}",
            f"  Sharpe:        {self.sharpe_ratio:.3f}" if self.sharpe_ratio else "  Sharpe:        N/A",
            f"  Sortino:       {self.sortino_ratio:.3f}" if self.sortino_ratio else "  Sortino:       N/A",
            f"  Max Drawdown:  {self.max_drawdown_pct:.1f}%",
            f"  Brier Score:   {self.brier_score:.4f}" if self.brier_score else "  Brier Score:   N/A",
            f"  Avg Edge:      {self.avg_edge:.4f}" if self.avg_edge else "  Avg Edge:      N/A",
            "────────────────────────────────────────",
        ]
        return "\n".join(lines)


def compute_metrics(
    y_true: np.ndarray,
    y_prob: np.ndarray,
    trades: pd.DataFrame | None = None,
    starting_bankroll: float = 1000.0,
) -> BacktestMetrics:
    """
    Compute all performance metrics.

    Args:
        y_true:           Ground truth binary outcomes (0/1)
        y_prob:           Model predicted probabilities
        trades:           DataFrame with columns: pnl, edge, bankroll (optional)
        starting_bankroll: Used for return% and drawdown calculations.

    Returns:
        BacktestMetrics dataclass
    """
    # Calibration metrics (always computable)
    brier = float(brier_score_loss(y_true, y_prob)) if len(y_true) > 0 else None

    # Default trade metrics
    total_pnl = 0.0
    total_return_pct = 0.0
    n_trades = 0
    win_rate = 0.0
    profit_factor = 0.0
    sharpe = None
    sortino = None
    max_dd = 0.0
    calmar = None
    avg_edge = None
    equity_curve = None

    if trades is not None and len(trades) > 0 and "pnl" in trades.columns:
        pnls = trades["pnl"].values
        n_trades = len(pnls)

        wins = pnls[pnls > 0]
        losses = pnls[pnls <= 0]

        total_pnl = float(pnls.sum())
        total_return_pct = 100 * total_pnl / starting_bankroll if starting_bankroll > 0 else 0.0
        win_rate = len(wins) / n_trades if n_trades > 0 else 0.0
        profit_factor = float(wins.sum() / (-losses.sum())) if losses.sum() != 0 else float("inf")
        avg_edge = float(trades["edge"].mean()) if "edge" in trades.columns else None

        # Sharpe (trade-level, annualized assuming trades are ~5min apart)
        if n_trades > 2:
            pnl_std = float(pnls.std())
            if pnl_std > 0:
                # Trades per year ≈ (252 days * 6.5 hrs * 12 trades/hr) for 5min markets
                trades_per_year = 252 * 288  # rough estimate
                sharpe = float((pnls.mean() / pnl_std) * np.sqrt(trades_per_year))

                downside = pnls[pnls < 0]
                if len(downside) > 0:
                    sortino = float((pnls.mean() / downside.std()) * np.sqrt(trades_per_year))

        # Max drawdown from equity curve
        if "bankroll" in trades.columns:
            equity = trades["bankroll"].values
            equity_curve = pd.Series(equity, index=trades.index if hasattr(trades, 'index') else None)
            max_dd = _max_drawdown_pct(equity)
            if max_dd > 0 and total_return_pct is not None:
                calmar = total_return_pct / max_dd

    return BacktestMetrics(
        total_pnl=total_pnl,
        total_return_pct=total_return_pct,
        n_trades=n_trades,
        win_rate=win_rate,
        profit_factor=profit_factor,
        sharpe_ratio=sharpe,
        sortino_ratio=sortino,
        max_drawdown_pct=max_dd,
        calmar_ratio=calmar,
        brier_score=brier,
        avg_edge=avg_edge,
        equity_curve=equity_curve,
    )


def _max_drawdown_pct(equity: np.ndarray) -> float:
    """Compute max drawdown as a percentage from peak."""
    if len(equity) < 2:
        return 0.0
    peak = np.maximum.accumulate(equity)
    drawdown = (equity - peak) / peak
    return float(-drawdown.min() * 100)


def print_fold_summary(fold_results: list) -> None:
    """Print a table of per-fold metrics."""
    try:
        from tabulate import tabulate
    except ImportError:
        for f in fold_results:
            print(f"Fold {f.fold_id}: {f.test_metrics}")
        return

    rows = []
    for f in fold_results:
        m = f.test_metrics
        rows.append([
            f.fold_id,
            str(f.test_start.date()),
            str(f.test_end.date()),
            f.n_trades,
            f"{m.win_rate:.1%}",
            f"{m.total_pnl:+.2f}",
            f"{m.sharpe_ratio:.3f}" if m.sharpe_ratio else "N/A",
            f"{m.max_drawdown_pct:.1f}%",
            f"{m.brier_score:.4f}" if m.brier_score else "N/A",
        ])

    headers = ["Fold", "Test Start", "Test End", "Trades", "Win%", "PnL", "Sharpe", "MaxDD", "Brier"]
    print(tabulate(rows, headers=headers, tablefmt="rounded_outline"))
