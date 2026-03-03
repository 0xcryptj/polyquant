"""
BaseService — abstract contract every runtime service must implement.

start()   — begin async work (idempotent)
stop()    — drain cleanly (must complete; orchestrator applies 30s timeout)
health()  — return HealthStatus (called every 60s by orchestrator)
status()  — return dict for /api/status and Telegram /health command
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from runtime.context import RuntimeContext


@dataclass
class HealthStatus:
    service:    str
    healthy:    bool
    details:    dict          = field(default_factory=dict)
    latency_ms: float         = 0.0
    checked_at: datetime      = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    def to_dict(self) -> dict:
        return {
            "service":    self.service,
            "healthy":    self.healthy,
            "details":    self.details,
            "latency_ms": round(self.latency_ms, 2),
            "checked_at": self.checked_at.isoformat(),
        }


class BaseService(ABC):
    def __init__(self, ctx: RuntimeContext) -> None:
        self.ctx         = ctx
        self._started    = False
        self._start_time: float | None = None

    # ── Abstract interface ────────────────────────────────────────────────────

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable service identifier (e.g. 'trading')."""
        ...

    @abstractmethod
    async def start(self) -> None:
        """Start the service. Must not block the event loop indefinitely."""
        ...

    @abstractmethod
    async def stop(self) -> None:
        """Stop the service gracefully. Orchestrator enforces 30s timeout."""
        ...

    @abstractmethod
    async def health(self) -> HealthStatus:
        """Return current health. Must return within 10s (orchestrator timeout)."""
        ...

    # ── Shared implementation ─────────────────────────────────────────────────

    def status(self) -> dict[str, Any]:
        uptime = (
            round(time.monotonic() - self._start_time, 1)
            if self._start_time is not None else None
        )
        return {
            "service":        self.name,
            "started":        self._started,
            "uptime_seconds": uptime,
        }

    # ── Helpers for subclasses ────────────────────────────────────────────────

    def _mark_started(self) -> None:
        self._started    = True
        self._start_time = time.monotonic()

    def _health_ok(self, **details: Any) -> HealthStatus:
        return HealthStatus(service=self.name, healthy=True, details=details)

    def _health_fail(self, reason: str, **details: Any) -> HealthStatus:
        return HealthStatus(
            service=self.name,
            healthy=False,
            details={"reason": reason, **details},
        )
