"""
ClobWalletProvider — SDK-based wallet provider (default).

Wraps:
  - wallets.wallet_manager.initialize_wallet()   (Polygon CLOB + AgentKit)
  - wallets.agentkit_base.get_usdc_balance()      (multi-chain balance)

This is the default Phase-2 provider: uses the Polymarket CLOB API credentials
and the optional Coinbase AgentKit SDK (if CDP keys are configured).

No new dependencies — everything is already in the repo.
"""
from __future__ import annotations

import logging
from typing import Any

from config.settings import settings
from wallets.providers.base import WalletProvider, WalletStatus

logger = logging.getLogger(__name__)


class ClobWalletProvider(WalletProvider):
    """
    Wallet provider backed by the existing SDK stack:
      - py-clob-client    (Polygon CLOB authentication)
      - coinbase-agentkit (Base + Polygon AgentKit, optional)
      - web3              (balance lookups via RPC)
    """

    name = "sdk"

    def __init__(self) -> None:
        self._bundle: Any = None   # WalletBundle from wallet_manager

    # ── Lazy init ─────────────────────────────────────────────────────────────

    def _get_bundle(self) -> Any:
        if self._bundle is None:
            from wallets.wallet_manager import initialize_wallet
            self._bundle = initialize_wallet()
        return self._bundle

    # ── WalletProvider interface ───────────────────────────────────────────────

    def address(self) -> str:
        return settings.wallet_address

    def balance(self, chain: str = "polygon") -> float:
        try:
            from wallets.agentkit_base import get_usdc_balance
            return get_usdc_balance(chain)
        except Exception as exc:
            logger.warning("balance(%s) failed: %s", chain, exc)
            return 0.0

    def ensure_funded(self, min_usdc: float, chain: str = "polygon") -> bool:
        current = self.balance(chain)
        if current >= min_usdc:
            return True
        logger.warning(
            "ensure_funded: need %.2f USDC on %s, have %.2f. "
            "Manual funding required (automated bridge not configured).",
            min_usdc, chain, current,
        )
        return False

    def health(self) -> bool:
        try:
            bundle = self._get_bundle()
            ok = bundle.clob_client.get_ok()
            return bool(ok)
        except Exception as exc:
            logger.warning("ClobWalletProvider health check failed: %s", exc)
            return False

    def status(self) -> WalletStatus:
        addr = self.address()
        poly_bal = self.balance("polygon")
        base_bal = self.balance("base")
        ok = self.health()

        return WalletStatus(
            healthy=ok,
            address=addr,
            provider=self.name,
            balances={"polygon": poly_bal, "base": base_bal},
            details={"clob_ok": ok},
        )
