"""
SupervisorService — independent risk monitoring layer.

Checks every CHECK_INTERVAL seconds:
  1. Kill switch triggered → pause trading + alert
  2. Consecutive loss streak ≥ MAX_CONSECUTIVE_LOSSES → pause + alert
  3. Streak clears (win after losses) → lift supervisor pause automatically

SupervisorService is the *only* component that auto-pauses trading based on
quantitative risk rules. Manual pause/resume is handled by TelegramService.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

import structlog

from runtime.context import RuntimeContext
from services.base import BaseService, HealthStatus
from paper_trading import persistence as db

log = structlog.get_logger("supervisor")

CHECK_INTERVAL           = 60   # seconds between risk checks
MAX_CONSECUTIVE_LOSSES   = 7    # pause trading after this many losses in a row
PROVIDER_UNHEALTHY_LIMIT = 3    # consecutive unhealthy checks before pausing


class SupervisorService(BaseService):
    name = "supervisor"

    def __init__(self, ctx: RuntimeContext) -> None:
        super().__init__(ctx)
        self._task:                  asyncio.Task | None = None
        self._consecutive_losses:    int   = 0
        self._paused_by_supervisor:  bool  = False
        self._last_check_at:         float = 0.0
        self._alerts_sent:           int   = 0
        self._provider_fail_count:   int   = 0   # consecutive unhealthy checks
        self._paused_by_provider:    bool  = False

    async def start(self) -> None:
        self._task = asyncio.create_task(self._monitor_loop(), name="supervisor")
        self._mark_started()
        log.info("started",
                 check_interval=CHECK_INTERVAL,
                 max_consecutive_losses=MAX_CONSECUTIVE_LOSSES)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        log.info("stopped", alerts_sent=self._alerts_sent)

    async def health(self) -> HealthStatus:
        if self._task and self._task.done():
            return self._health_fail("monitor loop dead")
        return self._health_ok(
            consecutive_losses=self._consecutive_losses,
            paused_by_supervisor=self._paused_by_supervisor,
        )

    def status(self) -> dict[str, Any]:
        base = super().status()
        base.update({
            "consecutive_losses":   self._consecutive_losses,
            "paused_by_supervisor": self._paused_by_supervisor,
            "paused_by_provider":   self._paused_by_provider,
            "provider_fail_count":  self._provider_fail_count,
            "alerts_sent":          self._alerts_sent,
            "last_check_at":        self._last_check_at,
        })
        return base

    # ── Monitor loop ──────────────────────────────────────────────────────────

    async def _monitor_loop(self) -> None:
        await asyncio.sleep(30)   # give engine time to warm up
        while not self.ctx.shutdown_event.is_set():
            try:
                await self._check()
                self._last_check_at = time.monotonic()
            except asyncio.CancelledError:
                return
            except Exception as exc:
                log.error("check_failed", error=str(exc))
            try:
                await asyncio.wait_for(
                    self.ctx.shutdown_event.wait(),
                    timeout=CHECK_INTERVAL,
                )
            except asyncio.TimeoutError:
                pass

    async def _check(self) -> None:
        engine = self.ctx.engine
        if engine is None:
            return

        # ── 1. Kill switch ────────────────────────────────────────────────────
        ks = getattr(engine, "_kill_switch", None)
        if ks is not None and ks.is_triggered() and self.ctx.trading_active.is_set():
            self.ctx.trading_active.clear()
            reason = ks.reason
            log.warning("kill_switch.triggered", reason=reason)
            await self._alert(
                f"🚨 *Kill switch triggered*\n\n"
                f"{reason}\n\n"
                f"Trading paused. Review drawdown, then /resume."
            )
            return

        # ── 2. Consecutive loss streak ────────────────────────────────────────
        streak = await asyncio.to_thread(self._count_consecutive_losses)
        self._consecutive_losses = streak

        if streak >= MAX_CONSECUTIVE_LOSSES and not self._paused_by_supervisor:
            self._paused_by_supervisor = True
            self.ctx.trading_active.clear()
            log.warning("streak.pause", streak=streak)
            await self._alert(
                f"⚠️ *Supervisor paused trading*\n\n"
                f"{streak} consecutive losses detected.\n"
                f"Review recent trades (/trades), then /resume."
            )

        # Lift supervisor pause if streak has reset
        if streak < MAX_CONSECUTIVE_LOSSES and self._paused_by_supervisor:
            self._paused_by_supervisor = False
            log.info("streak.cleared", streak=streak)

        # ── 3. Execution provider health ──────────────────────────────────────
        await self._check_providers()

    async def _check_providers(self) -> None:
        """
        Run health checks on WalletProvider and ExecutionProvider.

        After PROVIDER_UNHEALTHY_LIMIT consecutive failing checks,
        trading is paused and an alert is sent.  Auto-resumes when healthy.
        """
        ep = self.ctx.execution_provider
        wp = self.ctx.wallet_provider
        if ep is None and wp is None:
            return

        healthy = await asyncio.to_thread(self._providers_healthy, ep, wp)

        if not healthy:
            self._provider_fail_count += 1
            log.warning(
                "provider.unhealthy",
                fail_count=self._provider_fail_count,
                limit=PROVIDER_UNHEALTHY_LIMIT,
            )
            if (
                self._provider_fail_count >= PROVIDER_UNHEALTHY_LIMIT
                and not self._paused_by_provider
                and self.ctx.trading_active.is_set()
            ):
                self._paused_by_provider = True
                self.ctx.trading_active.clear()
                await self._alert(
                    "⚠️ *Provider health check failed*\n\n"
                    f"After {PROVIDER_UNHEALTHY_LIMIT} consecutive failures, "
                    "trading has been paused.\n"
                    "Check your wallet/execution provider, then /resume."
                )
        else:
            if self._paused_by_provider:
                self._paused_by_provider  = False
                self._provider_fail_count = 0
                log.info("provider.recovered")
            else:
                self._provider_fail_count = 0

    @staticmethod
    def _providers_healthy(ep: Any, wp: Any) -> bool:
        """Thread-safe provider health check (called via to_thread)."""
        if ep is not None:
            try:
                if not ep.health():
                    return False
            except Exception:
                return False
        if wp is not None:
            try:
                if not wp.health():
                    return False
            except Exception:
                return False
        return True

    def _count_consecutive_losses(self) -> int:
        """Count losses from the most recent resolved trades backward."""
        trades = db.get_all_closed_trades()
        if not trades:
            return 0
        resolved = [
            t for t in trades
            if t.get("status") in ("won", "lost") and t.get("resolved_at")
        ]
        resolved.sort(key=lambda t: t["resolved_at"], reverse=True)
        count = 0
        for t in resolved:
            if t["status"] == "lost":
                count += 1
            else:
                break
        return count

    async def _alert(self, message: str) -> None:
        self._alerts_sent += 1
        await self.ctx.send_alert(message)
