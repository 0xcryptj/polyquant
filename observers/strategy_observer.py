"""
Strategy Observer — runs multiple strategy modes concurrently (shadow tracks).

Each mode has its own min_edge and kelly; when the main engine opens a trade,
we record which modes "would have" taken it (edge >= mode.min_edge) and
apply the same PnL to those modes when the trade resolves. This finds the
most profitable parameter set without changing live params.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

STRATEGY_MODES = [
    {"id": "conservative", "label": "Conservative", "min_edge": 0.05, "kelly": 0.15},
    {"id": "default",      "label": "Default",      "min_edge": 0.03, "kelly": 0.25},
    {"id": "aggressive",   "label": "Aggressive",   "min_edge": 0.02, "kelly": 0.35},
]

STARTING_VIRTUAL_BALANCE = 1_000.0


@dataclass
class VirtualPosition:
    token_id: str
    direction: str
    entry_price: float
    size_usdc: float
    shares: float


@dataclass
class ModeState:
    mode_id: str
    label: str
    min_edge: float
    kelly: float
    balance: float
    starting_balance: float
    positions: list[VirtualPosition]
    closed_pnls: list[float]

    @property
    def total_pnl(self) -> float:
        return self.balance - self.starting_balance

    @property
    def n_trades(self) -> int:
        return len(self.closed_pnls)

    @property
    def win_rate(self) -> float:
        if not self.closed_pnls:
            return 0.0
        wins = sum(1 for p in self.closed_pnls if p > 0)
        return wins / len(self.closed_pnls)


class StrategyObserver:
    """
    Tracks virtual PnL per strategy mode. Feed trade_opened and trade_resolved
    events from the trading service.
    """

    def __init__(self) -> None:
        self._modes: dict[str, ModeState] = {}
        for m in STRATEGY_MODES:
            self._modes[m["id"]] = ModeState(
                mode_id=m["id"],
                label=m["label"],
                min_edge=float(m["min_edge"]),
                kelly=float(m["kelly"]),
                balance=STARTING_VIRTUAL_BALANCE,
                starting_balance=STARTING_VIRTUAL_BALANCE,
                positions=[],
                closed_pnls=[],
            )

    def on_trade_opened(self, event: dict) -> None:
        """Record which modes would have taken this trade (edge >= mode.min_edge)."""
        edge = float(event.get("edge", 0))
        token_id = str(event.get("token_id", ""))
        direction = str(event.get("direction", "YES"))
        size_usdc = float(event.get("size_usdc", 0))
        entry_price = float(event.get("entry_price", 0.5))
        if not entry_price:
            return
        shares = size_usdc / entry_price
        for state in self._modes.values():
            if edge >= state.min_edge:
                state.positions.append(VirtualPosition(
                    token_id=token_id,
                    direction=direction,
                    entry_price=entry_price,
                    size_usdc=size_usdc,
                    shares=shares,
                ))
                state.balance -= size_usdc

    def on_trade_resolved(self, event: dict) -> None:
        """Apply PnL to every mode that had a matching virtual position."""
        token_id = str(event.get("token_id", ""))
        direction = str(event.get("direction", "YES"))
        pnl = float(event.get("pnl", 0))
        for state in self._modes.values():
            for i, pos in enumerate(state.positions):
                if pos.token_id == token_id and pos.direction == direction:
                    state.balance += pos.size_usdc + pnl
                    state.closed_pnls.append(pnl)
                    state.positions.pop(i)
                    break

    def get_leaderboard(self) -> list[dict[str, Any]]:
        """Return modes sorted by total_pnl (best first) for API/UI."""
        out = []
        for m in STRATEGY_MODES:
            state = self._modes.get(m["id"])
            if not state:
                continue
            out.append({
                "id": state.mode_id,
                "label": state.label,
                "min_edge": state.min_edge,
                "kelly": state.kelly,
                "balance": round(state.balance, 2),
                "starting_balance": state.starting_balance,
                "total_pnl": round(state.total_pnl, 2),
                "n_trades": state.n_trades,
                "win_rate": round(state.win_rate, 4),
            })
        out.sort(key=lambda x: x["total_pnl"], reverse=True)
        return out
