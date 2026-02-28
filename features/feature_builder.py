"""
Feature engineering for the calibration model.

Builds a feature matrix from BTC OHLCV data + Polymarket market data:
  - Realized volatility (5m, 15m, 1h rolling windows)
  - Momentum (log-return over N bars)
  - ATR (Average True Range)
  - Bid-ask spread from order book
  - Time-of-day features
  - GARCH conditional volatility (when available)
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def build_features(
    ohlcv: pd.DataFrame,
    market_prices: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """
    Build the full feature matrix.

    Args:
        ohlcv:          BTC 1m OHLCV DataFrame (DatetimeIndex, UTC).
                        Required columns: open, high, low, close, volume
        market_prices:  Polymarket mid-price time series aligned to ohlcv index.
                        Optional; adds spread and polymarket-specific features.

    Returns:
        DataFrame with feature columns, same index as ohlcv.
        NaN rows at start (due to rolling windows) are NOT dropped here —
        label_builder and the walk-forward engine handle that.
    """
    _validate_ohlcv(ohlcv)
    df = ohlcv.copy()

    # Log returns
    df["log_return"] = np.log(df["close"] / df["close"].shift(1))

    # ── Realized Volatility ──────────────────────────────────────────────────
    # Annualized using sqrt(bars_per_year)
    bars_per_year = 525_600  # 1m bars
    ann = np.sqrt(bars_per_year)

    df["rv_5m"] = df["log_return"].rolling(5).std() * ann
    df["rv_15m"] = df["log_return"].rolling(15).std() * ann
    df["rv_1h"] = df["log_return"].rolling(60).std() * ann
    df["rv_4h"] = df["log_return"].rolling(240).std() * ann

    # ── Momentum ─────────────────────────────────────────────────────────────
    df["mom_5m"] = np.log(df["close"] / df["close"].shift(5))
    df["mom_15m"] = np.log(df["close"] / df["close"].shift(15))
    df["mom_1h"] = np.log(df["close"] / df["close"].shift(60))

    # ── ATR (Average True Range) ─────────────────────────────────────────────
    df["atr_14"] = _atr(df, window=14)
    df["atr_pct"] = df["atr_14"] / df["close"]  # normalized by price

    # ── Volume Features ───────────────────────────────────────────────────────
    df["vol_ratio"] = df["volume"] / df["volume"].rolling(60).mean()  # vs 1h avg

    # ── Price Distance from VWAP ─────────────────────────────────────────────
    df["vwap_1h"] = _vwap(df, window=60)
    df["price_vs_vwap"] = (df["close"] - df["vwap_1h"]) / df["vwap_1h"]

    # ── Bollinger Band Width ──────────────────────────────────────────────────
    bb_mean = df["close"].rolling(20).mean()
    bb_std = df["close"].rolling(20).std()
    df["bb_width"] = (2 * bb_std) / bb_mean  # normalized BB width

    # ── Time Features ─────────────────────────────────────────────────────────
    df["hour_sin"] = np.sin(2 * np.pi * df.index.hour / 24)
    df["hour_cos"] = np.cos(2 * np.pi * df.index.hour / 24)
    df["dow_sin"] = np.sin(2 * np.pi * df.index.dayofweek / 7)
    df["dow_cos"] = np.cos(2 * np.pi * df.index.dayofweek / 7)

    # ── Polymarket-specific Features ─────────────────────────────────────────
    if market_prices is not None:
        df = _add_market_features(df, market_prices)

    feature_cols = [
        "rv_5m", "rv_15m", "rv_1h", "rv_4h",
        "mom_5m", "mom_15m", "mom_1h",
        "atr_pct",
        "vol_ratio",
        "price_vs_vwap",
        "bb_width",
        "hour_sin", "hour_cos",
        "dow_sin", "dow_cos",
    ]
    if market_prices is not None:
        feature_cols += ["pm_spread", "pm_mid_price", "pm_price_momentum"]

    logger.info("Built %d features for %d bars", len(feature_cols), len(df))
    return df[feature_cols]


def _atr(df: pd.DataFrame, window: int = 14) -> pd.Series:
    """Wilder's Average True Range."""
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(window).mean()


def _vwap(df: pd.DataFrame, window: int = 60) -> pd.Series:
    """Rolling VWAP over `window` bars."""
    typical = (df["high"] + df["low"] + df["close"]) / 3
    return (typical * df["volume"]).rolling(window).sum() / df["volume"].rolling(window).sum()


def _add_market_features(df: pd.DataFrame, market_prices: pd.DataFrame) -> pd.DataFrame:
    """
    Add Polymarket-specific features after aligning to ohlcv index.

    market_prices should have columns: mid_price, spread (at minimum)
    """
    aligned = market_prices.reindex(df.index, method="ffill")

    if "spread" in aligned.columns:
        df["pm_spread"] = aligned["spread"]
    else:
        df["pm_spread"] = np.nan

    if "mid_price" in aligned.columns:
        df["pm_mid_price"] = aligned["mid_price"]
        df["pm_price_momentum"] = df["pm_mid_price"].pct_change(5)
    else:
        df["pm_mid_price"] = np.nan
        df["pm_price_momentum"] = np.nan

    return df


def _validate_ohlcv(df: pd.DataFrame) -> None:
    required = {"open", "high", "low", "close", "volume"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"OHLCV DataFrame missing columns: {missing}")
    if not isinstance(df.index, pd.DatetimeIndex):
        raise TypeError("OHLCV DataFrame must have a DatetimeIndex")


FEATURE_COLUMNS = [
    "rv_5m", "rv_15m", "rv_1h", "rv_4h",
    "mom_5m", "mom_15m", "mom_1h",
    "atr_pct",
    "vol_ratio",
    "price_vs_vwap",
    "bb_width",
    "hour_sin", "hour_cos",
    "dow_sin", "dow_cos",
]
