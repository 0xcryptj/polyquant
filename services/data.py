"""
DataService — background refresh of sentiment and wallet data.

Runs two independent async loops:
  sentiment_loop  every 3 min → ctx.sentiment_cache
  wallet_loop     every 5 min → ctx.wallet_report_cache

TradingService, TelegramService, and WebService all read from those caches.
No blocking calls touch the event loop directly.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

import structlog

from runtime.context import RuntimeContext
from services.base import BaseService, HealthStatus

log = structlog.get_logger("data")

SENTIMENT_INTERVAL = 180   # 3 minutes
WALLET_INTERVAL    = 300   # 5 minutes


class DataService(BaseService):
    name = "data"

    def __init__(self, ctx: RuntimeContext) -> None:
        super().__init__(ctx)
        self._tasks: list[asyncio.Task] = []
        self._sentiment_last:   float = 0.0
        self._wallet_last:      float = 0.0
        self._sentiment_errors: int   = 0
        self._wallet_errors:    int   = 0

    async def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._sentiment_loop(), name="data-sentiment"),
            asyncio.create_task(self._wallet_loop(),    name="data-wallets"),
        ]
        self._mark_started()
        log.info("started")

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        log.info("stopped")

    async def health(self) -> HealthStatus:
        now = time.monotonic()
        age = now - self._sentiment_last if self._sentiment_last else None
        if age is not None and age > SENTIMENT_INTERVAL * 3:
            return self._health_fail(
                "sentiment stale",
                age_s=round(age, 0),
                errors=self._sentiment_errors,
            )
        return self._health_ok(
            sentiment_age_s=round(age or 0, 0),
            sentiment_errors=self._sentiment_errors,
            wallet_errors=self._wallet_errors,
        )

    def status(self) -> dict[str, Any]:
        base = super().status()
        base.update({
            "sentiment_errors": self._sentiment_errors,
            "wallet_errors":    self._wallet_errors,
            "sentiment_last":   self._sentiment_last,
            "wallet_last":      self._wallet_last,
        })
        return base

    # ── Background loops ──────────────────────────────────────────────────────

    async def _sentiment_loop(self) -> None:
        await asyncio.sleep(5)   # let engine warm up first
        while not self.ctx.shutdown_event.is_set():
            try:
                snap = await asyncio.to_thread(
                    self.ctx.engine.get_sentiment_snapshot
                )
                self.ctx.sentiment_cache = snap
                self._sentiment_last     = time.monotonic()
                log.debug("sentiment.refreshed")
            except asyncio.CancelledError:
                return
            except Exception as exc:
                self._sentiment_errors += 1
                log.warning("sentiment.error", error=str(exc))
            await self._sleep(SENTIMENT_INTERVAL)

    async def _wallet_loop(self) -> None:
        await asyncio.sleep(20)
        while not self.ctx.shutdown_event.is_set():
            try:
                def _build() -> str:
                    from data.wallet_tracker import WalletTracker
                    return WalletTracker().build_report()
                report = await asyncio.to_thread(_build)
                self.ctx.wallet_report_cache = report
                self._wallet_last            = time.monotonic()
                log.debug("wallets.refreshed")
            except asyncio.CancelledError:
                return
            except Exception as exc:
                self._wallet_errors += 1
                log.warning("wallets.error", error=str(exc))
            await self._sleep(WALLET_INTERVAL)

    async def _sleep(self, seconds: float) -> None:
        """Sleep for `seconds`, but wake immediately on shutdown."""
        try:
            await asyncio.wait_for(
                self.ctx.shutdown_event.wait(),
                timeout=seconds,
            )
        except asyncio.TimeoutError:
            pass
