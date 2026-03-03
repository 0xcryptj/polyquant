"""
MockWalletProvider — paper mode wallet. No real funds.

Use when WALLET_PROVIDER=none or for paper trading only.
"""
from __future__ import annotations

from wallets.providers.base import WalletProvider, WalletStatus


class MockWalletProvider(WalletProvider):
    """Paper mode: returns placeholder values. Never touches real funds."""

    name = "mock"

    def status(self) -> WalletStatus:
        return WalletStatus(
            healthy=True,
            address="0x0000000000000000000000000000000000000000",
            provider=self.name,
            balances={"polygon": 0.0, "base": 0.0},
            details={"mode": "paper"},
        )

    def address(self) -> str:
        return "0x0000000000000000000000000000000000000000"

    def balance(self, chain: str = "polygon") -> float:
        return 0.0

    def ensure_funded(self, min_usdc: float, chain: str = "polygon") -> bool:
        return True  # Paper mode always "funded"

    def health(self) -> bool:
        return True
