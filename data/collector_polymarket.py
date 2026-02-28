"""
Polymarket CLOB order book snapshots and price history collector.

Polls the CLOB API to capture:
- Current best bid/ask for a token
- Mid-price time series
- Order book depth snapshots
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pandas as pd

from config.settings import settings

logger = logging.getLogger(__name__)

RAW_DIR = Path("data/raw/polymarket")
RAW_DIR.mkdir(parents=True, exist_ok=True)

CLOB_BASE = settings.polymarket_clob_host


@dataclass
class OrderBookSnapshot:
    """Point-in-time order book snapshot for a single token."""

    token_id: str
    timestamp: datetime
    best_bid: float
    best_ask: float
    mid_price: float
    spread: float
    bid_size: float
    ask_size: float
    raw: dict = field(default_factory=dict)

    @property
    def spread_pct(self) -> float:
        return self.spread / self.mid_price if self.mid_price > 0 else float("inf")


def _parse_book(token_id: str, book_data: dict) -> OrderBookSnapshot:
    """Parse raw CLOB order book response into an OrderBookSnapshot."""
    bids = book_data.get("bids", [])
    asks = book_data.get("asks", [])

    best_bid = float(bids[0]["price"]) if bids else 0.0
    best_ask = float(asks[0]["price"]) if asks else 1.0
    bid_size = float(bids[0]["size"]) if bids else 0.0
    ask_size = float(asks[0]["size"]) if asks else 0.0

    mid = (best_bid + best_ask) / 2
    spread = best_ask - best_bid

    return OrderBookSnapshot(
        token_id=token_id,
        timestamp=datetime.now(timezone.utc),
        best_bid=best_bid,
        best_ask=best_ask,
        mid_price=mid,
        spread=spread,
        bid_size=bid_size,
        ask_size=ask_size,
        raw=book_data,
    )


def get_order_book(token_id: str, client: httpx.Client | None = None) -> OrderBookSnapshot:
    """
    Fetch current order book for a token.

    Args:
        token_id: Polymarket CLOB token ID (condition_id + outcome index)
        client:   Optional reusable httpx.Client

    Returns:
        OrderBookSnapshot with best bid/ask/spread
    """
    url = f"{CLOB_BASE}/book"
    params = {"token_id": token_id}

    close_client = False
    if client is None:
        client = httpx.Client(timeout=10.0)
        close_client = True

    try:
        resp = client.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()
        return _parse_book(token_id, data)
    finally:
        if close_client:
            client.close()


def get_last_trade_price(token_id: str, client: httpx.Client | None = None) -> float | None:
    """Fetch last trade price for a token from the CLOB trades endpoint."""
    url = f"{CLOB_BASE}/last-trade-price"
    params = {"token_id": token_id}

    close_client = False
    if client is None:
        client = httpx.Client(timeout=10.0)
        close_client = True

    try:
        resp = client.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()
        price = data.get("price")
        return float(price) if price is not None else None
    except Exception as exc:
        logger.warning("Failed to get last trade price for %s: %s", token_id, exc)
        return None
    finally:
        if close_client:
            client.close()


def poll_snapshots(
    token_ids: list[str],
    interval_seconds: float = 30.0,
    max_snapshots: int | None = None,
) -> list[OrderBookSnapshot]:
    """
    Synchronously poll order book snapshots at fixed intervals.

    Args:
        token_ids:       List of token IDs to monitor.
        interval_seconds: Polling interval.
        max_snapshots:   Stop after this many rounds (None = run forever).

    Returns:
        List of snapshots collected (when max_snapshots is set).
    """
    snapshots: list[OrderBookSnapshot] = []
    rounds = 0

    with httpx.Client(timeout=10.0) as client:
        while max_snapshots is None or rounds < max_snapshots:
            for tid in token_ids:
                try:
                    snap = get_order_book(tid, client)
                    snapshots.append(snap)
                    logger.debug(
                        "Token %s: bid=%.4f ask=%.4f spread=%.4f",
                        tid[:12],
                        snap.best_bid,
                        snap.best_ask,
                        snap.spread,
                    )
                except Exception as exc:
                    logger.error("Error fetching book for %s: %s", tid[:12], exc)

            rounds += 1
            if max_snapshots is None or rounds < max_snapshots:
                time.sleep(interval_seconds)

    return snapshots


def snapshots_to_df(snapshots: list[OrderBookSnapshot]) -> pd.DataFrame:
    """Convert list of snapshots to a DataFrame indexed by timestamp."""
    rows = [
        {
            "timestamp": s.timestamp,
            "token_id": s.token_id,
            "best_bid": s.best_bid,
            "best_ask": s.best_ask,
            "mid_price": s.mid_price,
            "spread": s.spread,
            "bid_size": s.bid_size,
            "ask_size": s.ask_size,
        }
        for s in snapshots
    ]
    return pd.DataFrame(rows).set_index("timestamp")


def get_price_history(
    token_id: str,
    fidelity: int = 1,
    client: httpx.Client | None = None,
) -> pd.DataFrame:
    """
    Fetch Polymarket price history for a token.

    Args:
        token_id: Token ID to query.
        fidelity: Data granularity in minutes (1 or 5).

    Returns:
        DataFrame with columns [timestamp, price] indexed by timestamp.
    """
    url = f"{CLOB_BASE}/prices-history"
    params = {"market": token_id, "fidelity": fidelity}

    close_client = False
    if client is None:
        client = httpx.Client(timeout=15.0)
        close_client = True

    try:
        resp = client.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()

        history = data.get("history", [])
        if not history:
            logger.warning("Empty price history for token %s", token_id[:12])
            return pd.DataFrame(columns=["price"])

        df = pd.DataFrame(history)
        df["timestamp"] = pd.to_datetime(df["t"], unit="s", utc=True)
        df = df.rename(columns={"p": "price"}).drop(columns=["t"])
        df["price"] = df["price"].astype(float)
        df = df.set_index("timestamp").sort_index()
        return df
    finally:
        if close_client:
            client.close()


async def async_get_order_book(token_id: str, client: httpx.AsyncClient) -> OrderBookSnapshot | None:
    """Async version of get_order_book for concurrent polling."""
    url = f"{CLOB_BASE}/book"
    params = {"token_id": token_id}
    try:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        return _parse_book(token_id, resp.json())
    except Exception as exc:
        logger.error("Async book fetch failed for %s: %s", token_id[:12], exc)
        return None


async def async_poll_all(token_ids: list[str]) -> list[OrderBookSnapshot]:
    """Fetch all token order books concurrently."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        tasks = [async_get_order_book(tid, client) for tid in token_ids]
        results = await asyncio.gather(*tasks)
    return [r for r in results if r is not None]
