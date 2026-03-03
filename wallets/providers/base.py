"""
WalletProvider — abstract base class for all wallet backends.

Implementations:
  clob_wallet.py   — SDK path (wallet_manager + agentkit_base)  [default]
  coinbase_agentic.py — awal CLI path (Coinbase Agentic Wallets)

Usage in runtime:
  from wallets.providers.clob_wallet import ClobWalletProvider
  provider = ClobWalletProvider()
  balance = provider.balance("polygon")
  ok      = provider.ensure_funded(10.0)
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class WalletStatus:
    """Snapshot of wallet health and balances."""

    healthy:  bool
    address:  str
    provider: str
    balances: dict[str, float] = field(default_factory=dict)  # chain → USDC
    details:  dict[str, Any]   = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "healthy":  self.healthy,
            "address":  self.address,
            "provider": self.provider,
            "balances": self.balances,
            "details":  self.details,
        }


class WalletProvider(ABC):
    """
    Abstract wallet backend.

    All methods must be safe to call from a thread pool (no asyncio primitives).
    Heavy I/O (RPC calls, subprocess) must have timeouts.
    """

    name: str = "base"

    # ── Required ──────────────────────────────────────────────────────────────

    @abstractmethod
    def status(self) -> WalletStatus:
        """Return a health+balance snapshot. Must not raise."""

    @abstractmethod
    def address(self) -> str:
        """Return the primary wallet address."""

    @abstractmethod
    def balance(self, chain: str = "polygon") -> float:
        """
        Return USDC balance on the specified chain.

        Args:
            chain: "polygon" or "base"

        Returns:
            Balance as float. Returns 0.0 on any error (never raises).
        """

    @abstractmethod
    def ensure_funded(self, min_usdc: float, chain: str = "polygon") -> bool:
        """
        Ensure at least min_usdc USDC is available on the given chain.

        May trigger a bridge/transfer from Base if the chain balance is short.
        Returns True if funded (or already sufficient), False if not.
        Must NOT raise — log warnings and return False on failure.
        """

    @abstractmethod
    def health(self) -> bool:
        """
        Quick liveness check — called by SupervisorService every CHECK_INTERVAL.

        Returns True if the provider is operational. Should be fast (<5 s).
        """

    # ── Optional ──────────────────────────────────────────────────────────────

    def send(self, to: str, amount_usdc: float, chain: str = "polygon") -> str:
        """
        Send USDC to an address.

        Returns:
            Transaction hash string on success.

        Raises:
            NotImplementedError: if not supported by this provider.
        """
        raise NotImplementedError(f"{self.name} provider does not support send()")
