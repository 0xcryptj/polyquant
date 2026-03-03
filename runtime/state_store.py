"""
StateStore — persist runtime state in artifacts/state/runtime.json.

Used by orchestrator and services to persist:
  - enabled (paper loop running)
  - mode (paper|live)
  - last_error
  - last_tick
  - positions/orders snapshot (paper)
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# Project root: runtime/state_store.py -> runtime/ -> project root
PROJECT_ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS_DIR = PROJECT_ROOT / "artifacts"
STATE_DIR = ARTIFACTS_DIR / "state"
STATE_FILE = STATE_DIR / "runtime.json"


@dataclass
class RuntimeState:
    """Serializable runtime state."""

    enabled: bool = False
    mode: str = "paper"
    last_error: str = ""
    last_tick: str = ""
    positions: list[dict[str, Any]] = field(default_factory=list)
    orders_snapshot: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "RuntimeState":
        if not data:
            return cls()
        return cls(
            enabled=data.get("enabled", False),
            mode=data.get("mode", "paper"),
            last_error=data.get("last_error", ""),
            last_tick=data.get("last_tick", ""),
            positions=data.get("positions", []),
            orders_snapshot=data.get("orders_snapshot", []),
        )


class StateStore:
    """
    Thread-safe file-based state persistence.

    All methods are synchronous and safe to call from threads.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or STATE_FILE
        self._state = RuntimeState()

    def load(self) -> RuntimeState:
        """Load state from disk. Returns default if file missing or invalid."""
        if not self._path.exists():
            return self._state
        try:
            with open(self._path, encoding="utf-8") as f:
                data = json.load(f)
            self._state = RuntimeState.from_dict(data)
            return self._state
        except (json.JSONDecodeError, OSError):
            return self._state

    def save(self) -> None:
        """Write current state to disk."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump(self._state.to_dict(), f, indent=2)
        except OSError:
            pass  # Non-fatal; log if needed

    def update(
        self,
        *,
        enabled: bool | None = None,
        mode: str | None = None,
        last_error: str | None = None,
        last_tick: str | None = None,
        positions: list[dict[str, Any]] | None = None,
        orders_snapshot: list[dict[str, Any]] | None = None,
    ) -> None:
        """Merge updates and persist."""
        if enabled is not None:
            self._state.enabled = enabled
        if mode is not None:
            self._state.mode = mode
        if last_error is not None:
            self._state.last_error = last_error
        if last_tick is not None:
            self._state.last_tick = last_tick
        if positions is not None:
            self._state.positions = positions
        if orders_snapshot is not None:
            self._state.orders_snapshot = orders_snapshot
        self.save()

    def get(self) -> RuntimeState:
        """Return current in-memory state (does not reload from disk)."""
        return self._state


def get_state_store() -> StateStore:
    """Return singleton StateStore instance."""
    if not hasattr(get_state_store, "_instance"):
        get_state_store._instance = StateStore()  # type: ignore[attr-defined]
    return get_state_store._instance  # type: ignore[attr-defined]
