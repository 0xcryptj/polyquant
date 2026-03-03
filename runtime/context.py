"""
RuntimeContext — single source of truth for all live state.

Passed to every service. Services read/write through this typed object.
No bare module-level globals anywhere else in the runtime.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Coroutine

if TYPE_CHECKING:
    from paper_trading.engine import PaperEngine
    from paper_trading.learner import Learner
    from paper_trading.blotter import TradeBlotter
    from wallets.providers.base import WalletProvider
    from execution.providers.base import ExecutionProvider

BOT_VERSION = "2.0.0"


def _make_set_event() -> asyncio.Event:
    """Return an asyncio.Event that is already set (trading starts active)."""
    ev = asyncio.Event()
    ev.set()
    return ev


@dataclass
class RuntimeContext:
    # ── Lifecycle ─────────────────────────────────────────────────────────────
    # Set this to signal all services to stop
    shutdown_event: asyncio.Event = field(default_factory=asyncio.Event)
    # Set = trading enabled; clear = paused (supervisor or operator)
    trading_active: asyncio.Event = field(default_factory=_make_set_event)

    # ── Timing ────────────────────────────────────────────────────────────────
    start_time: float = field(default_factory=time.monotonic)

    # ── Error tracking ────────────────────────────────────────────────────────
    last_error: str = ""
    last_error_time: float = 0.0

    # ── Shared domain objects (injected by Orchestrator after construction) ───
    engine:  "PaperEngine | None" = None
    learner: "Learner | None"     = None

    # ── Provider layer (injected by Orchestrator) ─────────────────────────────
    wallet_provider:    "WalletProvider | None"    = None
    execution_provider: "ExecutionProvider | None" = None

    # ── Audit log ─────────────────────────────────────────────────────────────
    blotter: "TradeBlotter | None" = None

    # ── State store (persists enabled, mode, last_tick, etc.) ─────────────────
    state_store: "Any | None" = None

    # ── Data caches (written by DataService, read by Telegram + Web) ──────────
    sentiment_cache:      "dict | None" = None
    wallet_report_cache:  str           = "Wallet data loading…"

    # ── Inter-service notification hook ───────────────────────────────────────
    # TelegramService wires this so Supervisor can push alerts
    _telegram_notify: "Callable[[str], Coroutine[Any, Any, None]] | None" = field(
        default=None, repr=False
    )

    # ── Derived helpers ───────────────────────────────────────────────────────

    @property
    def uptime_seconds(self) -> float:
        return time.monotonic() - self.start_time

    @property
    def uptime_str(self) -> str:
        s = int(self.uptime_seconds)
        h, rem = divmod(s, 3600)
        m, sec = divmod(rem, 60)
        return f"{h}h {m}m" if h else f"{m}m {sec}s"

    def record_error(self, msg: str) -> None:
        self.last_error      = msg
        self.last_error_time = time.monotonic()
        if self.state_store is not None:
            self.state_store.update(last_error=msg)

    async def send_alert(self, text: str) -> None:
        """Dispatch an alert to Telegram if the hook is wired; otherwise no-op."""
        if self._telegram_notify is not None and text:
            try:
                await self._telegram_notify(text)
            except Exception:
                pass
