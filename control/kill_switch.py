"""
Daily drawdown kill switch — halt trading when daily loss limit is breached.

Referenced by execution/order_manager.py and paper_trading/engine.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone


@dataclass
class KillSwitch:
    """
    Monitors intraday drawdown and halts trading if the limit is breached.

    Args:
        starting_balance:   Initial bankroll for this session.
        max_drawdown_pct:   Fraction of daily-start balance to lose before halting
                            (e.g. 0.05 = 5%).
    """

    starting_balance: float
    max_drawdown_pct: float

    _triggered: bool = field(default=False, init=False)
    _reason: str = field(default="", init=False)
    _daily_start: float = field(default=0.0, init=False)
    _date: date = field(default_factory=lambda: datetime.now(timezone.utc).date(), init=False)

    def __post_init__(self) -> None:
        self._daily_start = self.starting_balance

    # ── Public Interface ──────────────────────────────────────────────────────

    def update(self, current_balance: float) -> None:
        """
        Call after every trade close or balance change.
        Resets daily baseline at midnight and fires if drawdown exceeds limit.
        """
        today = datetime.now(timezone.utc).date()
        if today != self._date:
            # New calendar day — reset baseline
            self._date = today
            self._daily_start = current_balance

        if self._triggered or self._daily_start <= 0:
            return

        drawdown = (self._daily_start - current_balance) / self._daily_start
        if drawdown >= self.max_drawdown_pct:
            self._triggered = True
            self._reason = (
                f"Daily drawdown {drawdown:.1%} ≥ limit {self.max_drawdown_pct:.1%}. "
                f"Daily start: ${self._daily_start:.2f} | Current: ${current_balance:.2f}"
            )

    def is_triggered(self) -> bool:
        return self._triggered

    @property
    def reason(self) -> str:
        return self._reason

    def reset(self, new_balance: float | None = None) -> None:
        """Manually reset the kill switch (use with caution)."""
        self._triggered = False
        self._reason = ""
        if new_balance is not None:
            self._daily_start = new_balance
            self.starting_balance = new_balance

    def current_drawdown(self, current_balance: float) -> float:
        """Return current drawdown fraction from daily start."""
        if self._daily_start <= 0:
            return 0.0
        return max(0.0, (self._daily_start - current_balance) / self._daily_start)
