"""Shared utilities for paper trading."""

from __future__ import annotations

from datetime import datetime, timezone


def age_seconds(opened_at_iso: str, now: datetime | None = None) -> float:
    """Return seconds since a trade was opened. Returns 0 on parse error."""
    now = now or datetime.now(timezone.utc)
    try:
        opened = datetime.fromisoformat(opened_at_iso)
        if opened.tzinfo is None:
            opened = opened.replace(tzinfo=timezone.utc)
        return (now - opened).total_seconds()
    except Exception:
        return 0.0


def split_message(text: str, max_len: int = 4000) -> list[str]:
    """Split text into Telegram-safe chunks (max 4096)."""
    return [text[i:i + max_len] for i in range(0, len(text), max_len)] if len(text) > max_len else [text]
