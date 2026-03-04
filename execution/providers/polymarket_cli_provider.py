"""
Polymarket CLI Verification Adapter — read-only healthcheck, list_markets, get_orderbook.

Used for verification and diagnostics. Does NOT place or cancel orders.
Use execution/providers/polymarket_cli.py (PolymarketCLIProvider) for full execution.

Environment:
  POLYMARKET_CLI_CMD or POLYMARKET_CLI_PATH — CLI command (e.g. polymarket-cli, npx @polymarket/clob-cli)
"""
from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path
from typing import Any

# Optional: load settings (may fail if config not fully initialized)
def _get_cli_cmd() -> str:
    try:
        from config.settings import settings
        return getattr(settings, "polymarket_cli_cmd", "") or ""
    except Exception:
        import os
        return os.environ.get("POLYMARKET_CLI_PATH") or os.environ.get("POLYMARKET_CLI_CMD") or ""


def _run_cli(*args: str, timeout: int = 15) -> dict[str, Any] | None:
    """Execute CLI with args. Returns parsed JSON or None on failure."""
    cmd_str = _get_cli_cmd()
    if not cmd_str:
        return None
    try:
        parts = shlex.split(cmd_str)
        result = subprocess.run(
            parts + list(args) + ["--json"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            return None
        out = (result.stdout or "").strip()
        if not out:
            return {"ok": True}
        return json.loads(out)
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def healthcheck() -> bool:
    """Return True if CLI responds successfully."""
    data = _run_cli("status", timeout=10)
    if data is None:
        data = _run_cli("markets", "list", timeout=10)
    return data is not None


def list_markets() -> list[dict[str, Any]]:
    """Return list of markets. Falls back to btc_markets.json if CLI unavailable."""
    data = _run_cli("markets", "list", timeout=15)
    if data and "markets" in data:
        return data["markets"]
    try:
        from config.settings import settings
        path = getattr(settings, "btc_markets_config_path", None)
        if path is None:
            path = Path(__file__).resolve().parent.parent.parent / "config" / "btc_markets.json"
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def get_orderbook(market_id: str) -> dict[str, Any] | None:
    """
    Fetch orderbook for market/token. Returns dict with bid, ask, mid, spread or None.
    """
    data = _run_cli("orderbook", "get", "--token-id", market_id, timeout=15)
    if data is None:
        return None
    bid = float(data.get("bid", data.get("bestBid", 0)) or 0)
    ask = float(data.get("ask", data.get("bestAsk", 0)) or 0)
    mid_raw = data.get("mid")
    if mid_raw is None:
        mid_raw = data.get("midPrice")
    mid = float(mid_raw) if mid_raw is not None else ((bid + ask) / 2 if ask > 0 else 0.0)
    return {
        "token_id": market_id,
        "bid": bid,
        "ask": ask,
        "mid": mid,
        "spread": ask - bid,
    }
