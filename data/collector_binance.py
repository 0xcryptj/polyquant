"""
Binance BTC/USDT 1-minute OHLCV collector via ccxt.

Fetches historical and live candle data with rate-limit-safe pagination.
Stores raw data as Parquet in data/raw/binance/.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import ccxt
import pandas as pd

from config.settings import settings

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

RAW_DIR = Path("data/raw/binance")
RAW_DIR.mkdir(parents=True, exist_ok=True)

# ccxt rate limit: Binance allows ~1200 requests/min, but we stay conservative
REQUEST_PAUSE_MS = 300


# Cache the working exchange across calls to avoid re-testing each time
_exchange_cache: ccxt.Exchange | None = None


def _build_exchange() -> ccxt.Exchange:
    """
    Build a ccxt exchange instance.

    Priority:
      1. Binance (global) — may be geo-blocked in some regions (451 error)
      2. Kraken  — no geo-restrictions, BTC/USDT available
    """
    global _exchange_cache
    if _exchange_cache is not None:
        return _exchange_cache

    try:
        exch = ccxt.binance(
            {
                "enableRateLimit": True,
                "options": {"defaultType": "spot"},
                "urls": {"api": {"rest": settings.binance_base_url}},
            }
        )
        # Lightweight test: fetch a single candle — throws 451 if geo-blocked
        exch.fetch_ohlcv("BTC/USDT", "1m", limit=1)
        logger.info("Using Binance for OHLCV data")
        _exchange_cache = exch
        return exch
    except Exception as exc:
        logger.warning("Binance unavailable (%s) — using Kraken", exc)

    kraken = ccxt.kraken({"enableRateLimit": True})
    logger.info("Using Kraken for OHLCV data")
    _exchange_cache = kraken
    return kraken


def fetch_ohlcv(
    symbol: str | None = None,
    timeframe: str | None = None,
    since_dt: datetime | None = None,
    limit: int = 1000,
) -> pd.DataFrame:
    """
    Fetch OHLCV candles from Binance.

    Args:
        symbol:    ccxt symbol, e.g. 'BTC/USDT' (defaults to settings)
        timeframe: e.g. '1m', '5m' (defaults to settings)
        since_dt:  start datetime (UTC). If None, fetches most recent `limit` candles.
        limit:     max candles per request (max 1000 for Binance)

    Returns:
        DataFrame with columns: [open_time, open, high, low, close, volume]
        Indexed by open_time (UTC-aware).
    """
    symbol = symbol or settings.btc_symbol
    timeframe = timeframe or settings.candle_interval
    exchange = _build_exchange()

    # Kraken uses BTC/USDT (same symbol, just need to load markets first)
    if isinstance(exchange, ccxt.kraken):
        exchange.load_markets()  # ensure market list is loaded

    since_ms: int | None = None
    if since_dt is not None:
        since_ms = int(since_dt.replace(tzinfo=timezone.utc).timestamp() * 1000)

    raw = exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=since_ms, limit=limit)

    if not raw:
        logger.warning("fetch_ohlcv returned empty result for %s %s", symbol, timeframe)
        return pd.DataFrame(columns=["open_time", "open", "high", "low", "close", "volume"])

    df = pd.DataFrame(raw, columns=["open_time_ms", "open", "high", "low", "close", "volume"])
    df["open_time"] = pd.to_datetime(df["open_time_ms"], unit="ms", utc=True)
    df = df.drop(columns=["open_time_ms"]).set_index("open_time")
    df = df.astype(float)

    logger.info("Fetched %d candles for %s %s", len(df), symbol, timeframe)
    return df


def fetch_ohlcv_paginated(
    symbol: str | None = None,
    timeframe: str | None = None,
    since_dt: datetime | None = None,
    until_dt: datetime | None = None,
    batch_size: int = 1000,
) -> pd.DataFrame:
    """
    Paginate through Binance API to fetch large historical datasets.

    Args:
        since_dt: Start datetime (UTC). Defaults to 30 days ago.
        until_dt: End datetime (UTC). Defaults to now.
        batch_size: Candles per API call.

    Returns:
        Combined DataFrame sorted by open_time ascending.
    """
    symbol = symbol or settings.btc_symbol
    timeframe = timeframe or settings.candle_interval
    exchange = _build_exchange()

    now = datetime.now(timezone.utc)
    if since_dt is None:
        since_dt = now - timedelta(days=30)
    if until_dt is None:
        until_dt = now

    since_ms = int(since_dt.timestamp() * 1000)
    until_ms = int(until_dt.timestamp() * 1000)

    all_candles: list[pd.DataFrame] = []
    cursor = since_ms

    logger.info("Starting paginated fetch: %s %s from %s to %s", symbol, timeframe, since_dt, until_dt)

    while cursor < until_ms:
        raw = exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=cursor, limit=batch_size)
        if not raw:
            break

        batch_df = pd.DataFrame(raw, columns=["open_time_ms", "open", "high", "low", "close", "volume"])
        batch_df["open_time"] = pd.to_datetime(batch_df["open_time_ms"], unit="ms", utc=True)
        batch_df = batch_df.drop(columns=["open_time_ms"]).set_index("open_time").astype(float)

        # Filter to requested range
        batch_df = batch_df[batch_df.index <= pd.Timestamp(until_dt)]
        if batch_df.empty:
            break

        all_candles.append(batch_df)
        cursor = int(batch_df.index[-1].timestamp() * 1000) + 1

        logger.debug("Fetched batch ending at %s (%d rows total)", batch_df.index[-1], sum(len(d) for d in all_candles))
        time.sleep(REQUEST_PAUSE_MS / 1000)

    if not all_candles:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    result = pd.concat(all_candles).sort_index().drop_duplicates()
    logger.info("Total fetched: %d candles", len(result))
    return result


def save_ohlcv(df: pd.DataFrame, filename: str | None = None) -> Path:
    """Save OHLCV DataFrame to Parquet in data/raw/binance/."""
    if filename is None:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        filename = f"btc_1m_{ts}.parquet"
    path = RAW_DIR / filename
    df.to_parquet(path)
    logger.info("Saved %d rows to %s", len(df), path)
    return path


def load_ohlcv(path: str | Path) -> pd.DataFrame:
    """Load OHLCV Parquet file."""
    df = pd.read_parquet(path)
    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index, utc=True)
    return df
