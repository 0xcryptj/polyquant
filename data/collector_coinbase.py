"""
Coinbase Exchange API — public BTC-USD 1-minute candles.

No API key required. Use when Binance is geo-blocked.
API: https://docs.cloud.coinbase.com/exchange/reference/exchangerestapi_getproductcandles
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import TYPE_CHECKING

import pandas as pd

from config.settings import settings

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# Max 300 candles per request
COINBASE_MAX_CANDLES = 300
# Granularity in seconds: 60 = 1m, 300 = 5m, etc.
GRANULARITY_1M = 60


def fetch_ohlcv(
    product_id: str = "BTC-USD",
    granularity: int = GRANULARITY_1M,
    limit: int = 300,
) -> pd.DataFrame | None:
    """
    Fetch OHLCV candles from Coinbase Exchange (public, no key).

    Args:
        product_id: e.g. BTC-USD
        granularity: bucket size in seconds (60 = 1m)
        limit: max candles (Coinbase max 300)

    Returns:
        DataFrame with index open_time (UTC), columns open, high, low, close, volume.
        None on error.
    """
    import httpx

    limit = min(limit, COINBASE_MAX_CANDLES)
    base = (settings.coinbase_exchange_url or "https://api.exchange.coinbase.com").rstrip("/")
    end = datetime.now(timezone.utc)
    start = end - timedelta(seconds=granularity * (limit + 1))
    # API expects ISO 8601
    start_str = start.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_str = end.strftime("%Y-%m-%dT%H:%M:%SZ")
    url = f"{base}/products/{product_id}/candles"
    params = {"granularity": granularity, "start": start_str, "end": end_str}

    try:
        with httpx.Client(timeout=15.0) as c:
            r = c.get(url, params=params)
            r.raise_for_status()
            data = r.json()
    except Exception as exc:
        logger.warning("Coinbase Exchange candles failed: %s", exc)
        return None

    if not data or not isinstance(data, list):
        return None

    # Response: [ [ time, low, high, open, close, volume? ], ... ] — oldest first
    rows = []
    for row in data:
        if not isinstance(row, (list, tuple)) or len(row) < 5:
            continue
        t, low, high, o, c = row[0], row[1], row[2], row[3], row[4]
        vol = float(row[5]) if len(row) > 5 else 0.0
        if isinstance(t, str):
            try:
                ts = datetime.fromisoformat(t.replace("Z", "+00:00"))
            except Exception:
                continue
        else:
            ts = datetime.fromtimestamp(int(t), tz=timezone.utc)
        rows.append({"open_time": ts, "open": float(o), "high": float(high), "low": float(low), "close": float(c), "volume": vol})

    if not rows:
        return None

    df = pd.DataFrame(rows).sort_values("open_time").drop_duplicates(subset=["open_time"]).set_index("open_time")
    df.index = pd.DatetimeIndex(df.index, tz=timezone.utc)
    df = df.tail(limit)
    logger.info("Fetched %d candles from Coinbase Exchange for %s", len(df), product_id)
    return df
