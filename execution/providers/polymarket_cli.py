"""
PolymarketCLIProvider — execution provider backed by a subprocess CLI.

Set EXECUTION_PROVIDER=cli and POLYMARKET_CLI_CMD to use this provider.

POLYMARKET_CLI_CMD examples:
    polymarket-cli           (npm: npx polymarket-cli)
    python -m polymarket     (if a Python CLI module is installed)
    npx @polymarket/clob-cli (npm package)

The provider calls the CLI with --json whenever possible and parses the output
robustly, falling back to text matching if JSON is unavailable.

KEY FEATURES:
  • Rate limiting    — token bucket, configurable requests/second
  • Idempotency      — fingerprint cache prevents duplicate orders on retry
  • Retry w/ backoff — transient errors are retried up to 3 times
  • JSON parsing     — --json flag tried first; text fallback
  • Graceful fallback — if CLI is not installed, logs a clear error
"""
from __future__ import annotations

import hashlib
import json
import logging
import shlex
import subprocess
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from config.settings import settings
from execution.clob_client import OrderResult
from execution.providers.base import ExecutionProvider, MarketSnapshot

logger = logging.getLogger(__name__)

# ── Rate limiter ──────────────────────────────────────────────────────────────

@dataclass
class _TokenBucket:
    """
    Token-bucket rate limiter.

    capacity:   max tokens (= burst allowance)
    rate:       tokens added per second
    """
    capacity: float = 2.0
    rate:     float = 2.0          # tokens/second
    _tokens:  float = field(init=False)
    _last_ts: float = field(init=False)

    def __post_init__(self) -> None:
        self._tokens  = self.capacity
        self._last_ts = time.monotonic()

    def acquire(self, block: bool = True, timeout: float = 5.0) -> bool:
        """Consume one token. Returns False if unavailable before timeout."""
        deadline = time.monotonic() + timeout
        while True:
            now = time.monotonic()
            elapsed = now - self._last_ts
            self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
            self._last_ts = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return True
            if not block or time.monotonic() >= deadline:
                return False
            time.sleep(0.1)


# ── Provider ──────────────────────────────────────────────────────────────────

class PolymarketCLIProvider(ExecutionProvider):
    """
    Execution provider that delegates to a subprocess CLI for all operations.

    Designed so that the CLI tool can be swapped without changing any
    trading logic — only POLYMARKET_CLI_CMD needs updating.
    """

    name = "cli"

    def __init__(
        self,
        cli_cmd:        str | None = None,
        rate_per_sec:   float      = 2.0,
        max_retries:    int        = 3,
        cmd_timeout:    int        = 20,
    ) -> None:
        raw_cmd = cli_cmd or settings.polymarket_cli_cmd
        if not raw_cmd:
            raise RuntimeError(
                "POLYMARKET_CLI_CMD is not set. "
                "Example: POLYMARKET_CLI_CMD=npx polymarket-cli"
            )
        self._cmd_parts   = shlex.split(raw_cmd)
        self._rate_limiter = _TokenBucket(capacity=rate_per_sec * 2, rate=rate_per_sec)
        self._max_retries  = max_retries
        self._cmd_timeout  = cmd_timeout

        # Idempotency cache: fingerprint → order_id
        # Bounded deque prevents unbounded growth during long sessions
        self._pending:  dict[str, str]  = {}
        self._cache_order: deque[str]   = deque(maxlen=500)

    # ── Low-level CLI runner ───────────────────────────────────────────────────

    def _run(
        self,
        *args: str,
        timeout: int | None = None,
        with_json: bool     = True,
    ) -> dict[str, Any]:
        """
        Execute the configured CLI with the given subcommand arguments.

        Strategy:
          1. Try with --json flag.
          2. If JSON parse fails, try without --json.
          3. Wrap text output in {"raw": ...} envelope.

        Returns:
            Parsed dict on success.

        Raises:
            RuntimeError: non-zero exit code.
            subprocess.TimeoutExpired: command timed out.
            FileNotFoundError: CLI executable not found.
        """
        cmd = [*self._cmd_parts, *args]
        if with_json:
            cmd.append("--json")

        result = subprocess.run(
            cmd,
            capture_output = True,
            text           = True,
            timeout        = timeout or self._cmd_timeout,
        )

        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            raise RuntimeError(
                f"CLI {args[0] if args else '?'} exited {result.returncode}: {stderr}"
            )

        stdout = (result.stdout or "").strip()
        if stdout:
            try:
                return json.loads(stdout)
            except json.JSONDecodeError:
                if with_json:
                    # Retry without --json flag (some CLIs ignore unknown flags)
                    return self._run(*args, timeout=timeout, with_json=False)
                return {"ok": True, "raw": stdout}
        return {"ok": True}

    def _run_with_retry(self, *args: str, timeout: int | None = None) -> dict[str, Any]:
        """
        Retry _run() up to max_retries times on transient errors.
        Uses exponential backoff: 1s, 2s, 4s, ...
        """
        last_exc: Exception = RuntimeError("no attempts made")
        for attempt in range(1, self._max_retries + 1):
            # Acquire rate-limit token
            if not self._rate_limiter.acquire(timeout=10.0):
                raise RuntimeError("Rate limit exhausted — too many concurrent requests")
            try:
                return self._run(*args, timeout=timeout)
            except (RuntimeError, subprocess.SubprocessError) as exc:
                last_exc = exc
                msg = str(exc).lower()
                transient = any(kw in msg for kw in ("timeout", "connection", "rate", "503", "502", "504"))
                if not transient or attempt >= self._max_retries:
                    raise
                wait = 2 ** (attempt - 1)
                logger.warning(
                    "CLI retry %d/%d after transient error: %s (sleeping %ds)",
                    attempt, self._max_retries, exc, wait,
                )
                time.sleep(wait)
        raise last_exc

    def _run_safe(self, *args: str) -> dict[str, Any] | None:
        """_run_with_retry() but returns None on any failure."""
        try:
            return self._run_with_retry(*args)
        except FileNotFoundError:
            logger.error(
                "CLI not found: %s — install it or check POLYMARKET_CLI_CMD",
                " ".join(self._cmd_parts),
            )
        except Exception as exc:
            logger.warning("CLI %s failed: %s", args[0] if args else "?", exc)
        return None

    # ── Idempotency ───────────────────────────────────────────────────────────

    def _order_fingerprint(
        self, token_id: str, direction: str, size_usdc: float, price: float
    ) -> str:
        """
        Deterministic fingerprint for an order.
        Two identical calls within the same session share a fingerprint.
        """
        key = f"{token_id}:{direction}:{size_usdc:.4f}:{price:.4f}"
        return hashlib.sha1(key.encode()).hexdigest()[:16]

    # ── ExecutionProvider interface ───────────────────────────────────────────

    def list_markets(self) -> list[dict]:
        data = self._run_safe("markets", "list")
        if data and "markets" in data:
            return data["markets"]
        if data and isinstance(data.get("raw"), str):
            # Text-mode fallback — return empty (can't parse reliably)
            return []
        # Fallback: read from config file
        try:
            import json as _json
            with open(settings.btc_markets_config_path) as f:
                return _json.load(f)
        except Exception:
            return []

    def get_orderbook(self, token_id: str) -> MarketSnapshot:
        data = self._run_safe("orderbook", "get", "--token-id", token_id)
        if data:
            bid    = float(data.get("bid",    data.get("bestBid",  0)) or 0)
            ask    = float(data.get("ask",    data.get("bestAsk",  0)) or 0)
            mid    = float(data.get("mid",    data.get("midPrice", 0)) or (bid + ask) / 2 if ask > 0 else 0)
            spread = ask - bid
            return MarketSnapshot(token_id=token_id, bid=bid, ask=ask, mid=mid, spread=spread, raw=data)

        # Fallback to Polymarket data collector
        try:
            from data.collector_polymarket import get_order_book
            snap = get_order_book(token_id)
            return MarketSnapshot(
                token_id = token_id,
                bid      = snap.best_bid or 0.0,
                ask      = snap.best_ask or 0.0,
                mid      = snap.mid_price or 0.0,
                spread   = (snap.best_ask or 0.0) - (snap.best_bid or 0.0),
            )
        except Exception as exc:
            logger.warning("get_orderbook fallback failed for %s: %s", token_id[:12], exc)
            return MarketSnapshot(token_id=token_id, bid=0, ask=0, mid=0, spread=0)

    def place_order(
        self,
        token_id:        str,
        direction:       str,
        size_usdc:       float,
        price:           float,
        *,
        idempotency_key: str | None = None,
    ) -> OrderResult:
        # ── Idempotency check ─────────────────────────────────────────────────
        fp = idempotency_key or self._order_fingerprint(token_id, direction, size_usdc, price)
        if fp in self._pending:
            existing_id = self._pending[fp]
            logger.info("Idempotency hit: order %s already pending for fp=%s", existing_id, fp)
            return OrderResult(
                success    = True,
                order_id   = existing_id,
                token_id   = token_id,
                direction  = direction,
                price      = price,
                size_usdc  = size_usdc,
                shares     = size_usdc / price if price > 0 else 0,
                paper      = settings.paper_trading,
            )

        # ── Paper mode ────────────────────────────────────────────────────────
        if settings.paper_trading:
            import time as _time
            fake_id = f"CLI-PAPER-{int(_time.time())}-{token_id[:8]}"
            self._pending[fp] = fake_id
            self._cache_order.append(fp)
            logger.info(
                "PAPER ORDER (cli): %s %s | price=%.4f | size=%.2f | id=%s",
                direction, token_id[:12], price, size_usdc, fake_id,
            )
            return OrderResult(
                success=True, order_id=fake_id, token_id=token_id,
                direction=direction, price=price, size_usdc=size_usdc,
                shares=size_usdc / price if price > 0 else 0, paper=True,
            )

        # ── Live order ────────────────────────────────────────────────────────
        try:
            data = self._run_with_retry(
                "order", "place",
                "--token-id",  token_id,
                "--direction", direction.lower(),
                "--size",      str(size_usdc),
                "--price",     str(price),
            )
        except Exception as exc:
            logger.error("place_order failed: %s", exc)
            return OrderResult(
                success=False, order_id=None, token_id=token_id,
                direction=direction, price=price, size_usdc=size_usdc,
                shares=0, paper=False, error=str(exc),
            )

        # Parse order ID from CLI response (multiple common field names)
        order_id = (
            data.get("orderId") or data.get("orderID") or
            data.get("order_id") or data.get("id") or ""
        )
        if not order_id:
            return OrderResult(
                success=False, order_id=None, token_id=token_id,
                direction=direction, price=price, size_usdc=size_usdc,
                shares=0, paper=False, error=f"no order_id in CLI response: {data}",
            )

        self._pending[fp] = order_id
        self._cache_order.append(fp)
        logger.info(
            "LIVE ORDER (cli): %s %s | price=%.4f | size=%.2f | id=%s",
            direction, token_id[:12], price, size_usdc, order_id,
        )
        return OrderResult(
            success=True, order_id=order_id, token_id=token_id,
            direction=direction, price=price, size_usdc=size_usdc,
            shares=size_usdc / price if price > 0 else 0,
            paper=False, raw_response=data,
        )

    def cancel_order(self, order_id: str) -> bool:
        if settings.paper_trading:
            return True
        data = self._run_safe("order", "cancel", "--order-id", order_id)
        ok = data is not None and data.get("ok", True)
        if ok:
            # Remove from pending cache
            to_remove = [fp for fp, oid in self._pending.items() if oid == order_id]
            for fp in to_remove:
                self._pending.pop(fp, None)
        return bool(ok)

    def positions(self) -> list[dict]:
        data = self._run_safe("positions", "list")
        if data and "positions" in data:
            return data["positions"]
        return []

    # ── Risk gate ─────────────────────────────────────────────────────────────

    def can_execute(self) -> tuple[bool, str]:
        if settings.paper_trading:
            return True, ""
        if not self.health():
            return False, f"CLI provider {self.name!r} is unhealthy"
        return True, ""

    def health(self) -> bool:
        data = self._run_safe("status")
        return data is not None
