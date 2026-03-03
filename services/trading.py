"""
TradingService — runs the engine cycle loop every LOOP_INTERVAL seconds.

Responsibilities:
  - Call engine.run_cycle() in a thread pool (never blocks event loop)
  - Dispatch trade events as Telegram alerts via ctx.send_alert()
  - Trigger adaptive learning after each cycle
  - Respect ctx.trading_active (pause/resume from Telegram or Supervisor)
  - Record cycle metrics for health checks and /status
"""
from __future__ import annotations

import asyncio
import time
import traceback
from typing import Any

import structlog

from runtime.context import RuntimeContext
from services.base import BaseService, HealthStatus

log = structlog.get_logger("trading")

LOOP_INTERVAL = 25    # seconds between trading cycles
STARTUP_DELAY = 12    # seconds after boot before the first cycle


class TradingService(BaseService):
    name = "trading"

    def __init__(self, ctx: RuntimeContext) -> None:
        super().__init__(ctx)
        self._task:           asyncio.Task | None = None
        self._last_cycle_at:  float = 0.0
        self._last_cycle_sec: float = 0.0
        self._cycles_total:   int   = 0
        self._cycle_errors:   int   = 0

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="trading-loop")
        self._mark_started()
        log.info("started", interval=LOOP_INTERVAL)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        log.info("stopped",
                 cycles=self._cycles_total,
                 errors=self._cycle_errors)

    async def health(self) -> HealthStatus:
        if not self._started:
            return self._health_fail("not started")
        if self._task and self._task.done():
            exc = (
                self._task.exception()
                if not self._task.cancelled() else None
            )
            return self._health_fail(
                "loop dead",
                exception=str(exc) if exc else "cancelled",
            )
        stale = LOOP_INTERVAL * 4
        if self._last_cycle_at and (time.monotonic() - self._last_cycle_at) > stale:
            return self._health_fail(
                "stale",
                seconds_since_last=round(time.monotonic() - self._last_cycle_at),
            )
        return self._health_ok(
            last_cycle_sec=self._last_cycle_sec,
            cycles=self._cycles_total,
            errors=self._cycle_errors,
        )

    def status(self) -> dict[str, Any]:
        base = super().status()
        base.update({
            "cycles_total":   self._cycles_total,
            "cycle_errors":   self._cycle_errors,
            "last_cycle_sec": round(self._last_cycle_sec, 1),
            "trading_active": self.ctx.trading_active.is_set(),
        })
        return base

    # ── Loop ──────────────────────────────────────────────────────────────────

    async def _loop(self) -> None:
        log.info("loop.begin")
        await asyncio.sleep(STARTUP_DELAY)

        while not self.ctx.shutdown_event.is_set():

            # ── Pause check ───────────────────────────────────────────────────
            if not self.ctx.trading_active.is_set():
                await self._sleep(5)
                continue

            t0 = time.monotonic()

            # ── Execution provider risk gate ──────────────────────────────────
            ep = self.ctx.execution_provider
            if ep is not None:
                can_exec, reason = await asyncio.to_thread(ep.can_execute)
                if not can_exec:
                    log.warning("provider.vetoed", reason=reason)
                    if self.ctx.blotter:
                        self.ctx.blotter.record_provider_veto(
                            provider  = ep.name,
                            reason    = reason,
                            token_id  = "",
                            size_usdc = 0.0,
                        )
                    await self.ctx.send_alert(
                        f"⚠️ *Execution provider vetoed cycle*\n{reason}"
                    )
                    await self._sleep(30)
                    continue

            # ── Run cycle in thread pool (never blocks event loop) ─────────────
            try:
                events = await asyncio.to_thread(self.ctx.engine.run_cycle)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._cycle_errors += 1
                msg = f"{type(exc).__name__}: {exc}"
                self.ctx.record_error(msg)
                log.error("cycle.error",
                           error=msg,
                           traceback=traceback.format_exc())
                await self._sleep(10)
                continue

            # ── Metrics ───────────────────────────────────────────────────────
            elapsed               = time.monotonic() - t0
            self._last_cycle_at   = time.monotonic()
            self._last_cycle_sec  = elapsed
            self._cycles_total   += 1

            opened   = sum(1 for e in events if e.get("type") == "trade_opened")
            resolved = sum(1 for e in events if e.get("type") == "trade_resolved")

            log.info("cycle.done",
                     cycle=self._cycles_total,
                     elapsed=round(elapsed, 1),
                     opened=opened,
                     resolved=resolved,
                     balance=round(self.ctx.engine.balance, 2))

            # ── Update state store ────────────────────────────────────────────
            ss = getattr(self.ctx, "state_store", None)
            if ss is not None:
                from datetime import datetime, timezone
                last_tick = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                positions = []
                try:
                    raw = self.ctx.engine.get_open_positions_for_display()
                    for p in raw:
                        snap = {
                            "id": p.get("id"),
                            "direction": p.get("direction"),
                            "size_usdc": p.get("size_usdc"),
                            "btc_price_entry": p.get("btc_price_entry"),
                        }
                        if "opened_at" in p:
                            ot = p["opened_at"]
                            snap["opened_at"] = ot.isoformat() if hasattr(ot, "isoformat") else str(ot)
                        positions.append(snap)
                except Exception:
                    pass
                ss.update(
                    last_tick=last_tick,
                    enabled=self.ctx.trading_active.is_set(),
                    positions=positions,
                )

            # ── Dispatch events ───────────────────────────────────────────────
            for event in events:
                await self._dispatch(event)

            # ── Adaptive learning ─────────────────────────────────────────────
            await self._maybe_learn()

            # ── Sleep the remainder of the interval ───────────────────────────
            spent = time.monotonic() - t0
            await self._sleep(max(0.0, LOOP_INTERVAL - spent))

        log.info("loop.end")

    async def _dispatch(self, event: dict) -> None:
        etype = event.get("type")

        # ── Blotter recording ──────────────────────────────────────────────────
        blotter = self.ctx.blotter
        if blotter is not None:
            if etype == "trade_opened":
                blotter.record_order_placed(
                    order_id  = str(event.get("trade_id", "")),
                    token_id  = str(event.get("token_id", "")),
                    direction = str(event.get("direction", "")),
                    size_usdc = float(event.get("size_usdc", 0)),
                    price     = float(event.get("entry_price", 0)),
                    edge      = float(event.get("edge", 0)),
                    provider  = getattr(self.ctx.execution_provider, "name", "clob"),
                    paper     = bool(event.get("simulated", True)),
                )
            elif etype == "trade_resolved":
                blotter.record_order_filled(
                    order_id   = str(event.get("trade_id", "")),
                    pnl        = float(event.get("pnl", 0)),
                    exit_price = float(event.get("exit_price", 0)),
                    balance    = float(event.get("balance", 0)),
                    won        = bool(event.get("won", False)),
                )
            elif etype == "kill_switch":
                blotter.record_kill_switch(
                    reason  = str(event.get("reason", "")),
                    balance = float(event.get("balance", 0)),
                )

        # Kill switch: pause trading immediately
        if etype == "kill_switch":
            self.ctx.trading_active.clear()
            self.ctx.record_error(
                f"Kill switch: {event.get('reason', '')}"
            )
            log.warning("kill_switch.triggered",
                        reason=event.get("reason"))

        # Forward all events as alerts
        text = _format_event(event)
        if text:
            await self.ctx.send_alert(text)

    async def _maybe_learn(self) -> None:
        try:
            insight = await asyncio.to_thread(self.ctx.learner.maybe_learn)
            if insight and insight.get("n", 0) > 0:
                n        = insight["n"]
                win_rate = round(insight.get("win_rate", 0), 3)
                log.info("learner.updated", n=n, win_rate=win_rate)
                if self.ctx.blotter:
                    self.ctx.blotter.record_learner_updated(
                        n        = n,
                        win_rate = win_rate,
                        min_edge = insight.get("min_edge"),
                        kelly    = insight.get("kelly_fraction"),
                    )
        except Exception as exc:
            log.warning("learner.error", error=str(exc))

    async def _sleep(self, seconds: float) -> None:
        """Sleep, but wake immediately if shutdown is signalled."""
        try:
            await asyncio.wait_for(
                self.ctx.shutdown_event.wait(),
                timeout=seconds,
            )
        except asyncio.TimeoutError:
            pass


# ── Event formatters ──────────────────────────────────────────────────────────

def _format_event(event: dict) -> str:
    etype = event.get("type")

    if etype == "trade_opened":
        d   = "UP" if event.get("direction") == "YES" else "DOWN"
        sim = " _(sim)_" if event.get("simulated") else ""
        return (
            f"📤 *Trade #{event.get('trade_id')} opened*{sim}\n"
            f"{d}  ${event.get('size_usdc', 0):.2f}  "
            f"edge={event.get('edge', 0):.3f}  "
            f"BTC=${event.get('btc_price', 0):,.0f}"
        )

    if etype == "trade_resolved":
        icon = "✅" if event.get("won") else "❌"
        pnl  = event.get("pnl", 0)
        d    = "UP" if event.get("direction") == "YES" else "DOWN"
        return (
            f"{icon} *Trade #{event.get('trade_id')} closed*  {d}  "
            f"{pnl:+.2f}  bal=${event.get('balance', 0):.2f}"
        )

    if etype == "kill_switch":
        return (
            f"🚨 *Safety stop triggered*\n\n"
            f"{event.get('reason', '')}\n"
            f"Balance: ${event.get('balance', 0):.2f}\n\n"
            f"Use /resume after reviewing."
        )

    return ""
