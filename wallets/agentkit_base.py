"""
Coinbase AgentKit — Base Chain Wallet Provider.

This module manages the AgentKit wallet on Base (chain_id=8453), which is
Coinbase's L2 for gas-efficient USDC transfers. When transitioning from
paper trading to live trading, USDC is held on Base and bridged/transferred
to Polygon for Polymarket order margin.

Supported operations:
  - Check USDC balance on Base
  - Transfer USDC from Base → Polygon (via Coinbase's cross-chain infra)
  - Sign Polymarket CLOB orders (using the same private key)
  - Interact with Polymarket's CTF Exchange on Polygon

Chain reference:
  Base mainnet:    chain_id=8453,   USDC=0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913
  Polygon mainnet: chain_id=137,    USDC=0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from config.settings import settings

logger = logging.getLogger(__name__)

# ── Chain Constants ───────────────────────────────────────────────────────────

BASE_CHAIN_ID       = 8453
BASE_RPC_URL        = settings.base_rpc_url
BASE_USDC_ADDRESS   = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

POLYGON_CHAIN_ID    = settings.POLYGON_CHAIN_ID
POLYGON_RPC_URL     = settings.polygon_rpc_url
POLYGON_USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"


@dataclass
class MultiChainBundle:
    """Wallet instances across Base and Polygon chains."""

    # Base chain (AgentKit primary — USDC funding source)
    base_provider: Any | None
    base_agent_kit: Any | None

    # Polygon chain (Polymarket trading chain)
    polygon_provider: Any | None
    polygon_agent_kit: Any | None
    polygon_clob_client: Any | None  # py-clob-client

    wallet_address: str


def initialize_multi_chain() -> MultiChainBundle:
    """
    Initialize wallet providers on both Base and Polygon.

    Returns:
        MultiChainBundle with both chain providers and CLOB client.

    Note:
        Both chains use the same private key / wallet address.
        CDP AgentKit manages gas on Base; manual gas on Polygon (MATIC).
    """
    private_key = settings.wallet_private_key.get_secret_value()

    base_provider    = None
    base_agent_kit   = None
    polygon_provider = None
    polygon_agent_kit = None
    polygon_clob     = None

    # ── Base chain via AgentKit ───────────────────────────────────────────
    if settings.cdp_api_key_id and settings.cdp_api_key_secret:
        try:
            base_provider, base_agent_kit = _init_agentkit_chain(
                private_key=private_key,
                rpc_url=BASE_RPC_URL,
                chain_id=BASE_CHAIN_ID,
            )
            logger.info("AgentKit initialized on Base (chain_id=%d)", BASE_CHAIN_ID)
        except Exception as exc:
            logger.warning("Base chain AgentKit init failed: %s", exc)

        try:
            polygon_provider, polygon_agent_kit = _init_agentkit_chain(
                private_key=private_key,
                rpc_url=POLYGON_RPC_URL,
                chain_id=POLYGON_CHAIN_ID,
            )
            logger.info("AgentKit initialized on Polygon (chain_id=%d)", POLYGON_CHAIN_ID)
        except Exception as exc:
            logger.warning("Polygon AgentKit init failed: %s", exc)
    else:
        logger.info("CDP credentials not set — AgentKit skipped (paper trading ok)")

    # ── Polygon CLOB client (for Polymarket orders) ───────────────────────
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds

        polygon_clob = ClobClient(
            host=settings.polymarket_clob_host,
            chain_id=POLYGON_CHAIN_ID,
            key=private_key,
            creds=ApiCreds(
                api_key=settings.polymarket_api_key,
                api_secret=settings.polymarket_api_secret.get_secret_value(),
                api_passphrase=settings.polymarket_api_passphrase.get_secret_value(),
            ),
        )
        logger.info("Polymarket CLOB client initialized")
    except ImportError:
        logger.warning("py-clob-client not installed — live trading disabled")
    except Exception as exc:
        logger.warning("CLOB client init failed: %s", exc)

    return MultiChainBundle(
        base_provider=base_provider,
        base_agent_kit=base_agent_kit,
        polygon_provider=polygon_provider,
        polygon_agent_kit=polygon_agent_kit,
        polygon_clob_client=polygon_clob,
        wallet_address=settings.wallet_address,
    )


def _init_agentkit_chain(
    private_key: str,
    rpc_url: str,
    chain_id: int,
) -> tuple[Any, Any]:
    """Initialize an AgentKit EthAccountWalletProvider on a specific chain."""
    from coinbase_agentkit import (
        AgentKit,
        AgentKitConfig,
        EthAccountWalletProvider,
        EthAccountWalletProviderConfig,
    )

    provider_config = EthAccountWalletProviderConfig(
        private_key=private_key,
        rpc_url=rpc_url,
        chain_id=chain_id,
    )
    provider = EthAccountWalletProvider(config=provider_config)

    kit_config = AgentKitConfig(
        cdp_api_key_id=settings.cdp_api_key_id,
        cdp_api_key_secret=(
            settings.cdp_api_key_secret.get_secret_value()
            if settings.cdp_api_key_secret else None
        ),
        wallet_provider=provider,
    )
    kit = AgentKit(config=kit_config)
    return provider, kit


# ── Balance Checks ────────────────────────────────────────────────────────────

def get_usdc_balance(chain: str = "base") -> float:
    """
    Get USDC balance on the specified chain.

    Args:
        chain: "base" or "polygon"

    Returns:
        USDC balance as float (0.0 on error)
    """
    try:
        from web3 import Web3

        if chain == "base":
            rpc = BASE_RPC_URL
            usdc_addr = BASE_USDC_ADDRESS
        else:
            rpc = POLYGON_RPC_URL
            usdc_addr = POLYGON_USDC_ADDRESS

        w3 = Web3(Web3.HTTPProvider(rpc))
        addr = Web3.to_checksum_address(settings.wallet_address)
        usdc_addr = Web3.to_checksum_address(usdc_addr)

        abi = [{
            "constant": True,
            "inputs": [{"name": "_owner", "type": "address"}],
            "name": "balanceOf",
            "outputs": [{"name": "balance", "type": "uint256"}],
            "type": "function",
        }]
        contract = w3.eth.contract(address=usdc_addr, abi=abi)
        raw = contract.functions.balanceOf(addr).call()
        return raw / 1e6  # USDC has 6 decimals

    except Exception as exc:
        logger.warning("USDC balance check failed on %s: %s", chain, exc)
        return 0.0


def get_all_balances() -> dict[str, float]:
    """Return USDC balances across all supported chains."""
    return {
        "base_usdc": get_usdc_balance("base"),
        "polygon_usdc": get_usdc_balance("polygon"),
    }


def bridge_usdc_to_polygon(amount_usdc: float, bundle: MultiChainBundle) -> bool:
    """
    Initiate a USDC transfer from Base to Polygon for Polymarket trading.

    NOTE: This is a placeholder — actual bridging requires Coinbase Onramp API
    or the Circle CCTP bridge. For now, this logs the intent and returns False.
    Manual bridging via Coinbase Exchange is recommended until CCTP is integrated.

    Args:
        amount_usdc: Amount to bridge
        bundle:      MultiChainBundle with initialized providers

    Returns:
        True on success, False if not yet implemented
    """
    logger.warning(
        "Bridge %.2f USDC Base→Polygon requested.\n"
        "Automated bridging not yet implemented.\n"
        "Manual steps:\n"
        "  1. Go to https://bridge.base.org\n"
        "  2. Bridge USDC from Base to Polygon\n"
        "  3. Or use Coinbase Exchange: Withdraw USDC → Polygon network\n"
        "Amount: %.2f USDC",
        amount_usdc, amount_usdc,
    )
    return False


def live_trading_readiness_check() -> dict[str, Any]:
    """
    Check if the system is ready to switch from paper to live trading.

    Returns a dict with readiness status for each requirement.
    """
    checks: dict[str, Any] = {}

    # 1. CLOB credentials
    try:
        from py_clob_client.client import ClobClient
        checks["py_clob_client_installed"] = True
    except ImportError:
        checks["py_clob_client_installed"] = False

    # 2. Polygon USDC balance
    polygon_usdc = get_usdc_balance("polygon")
    checks["polygon_usdc_balance"] = polygon_usdc
    checks["polygon_usdc_sufficient"] = polygon_usdc >= 10.0

    # 3. Base USDC balance (for funding)
    base_usdc = get_usdc_balance("base")
    checks["base_usdc_balance"] = base_usdc

    # 4. API credentials set
    checks["clob_api_key_set"] = bool(settings.polymarket_api_key and
                                       settings.polymarket_api_key != "your_polymarket_api_key_here")
    checks["wallet_address_set"] = settings.wallet_address != "0x0000000000000000000000000000000000000000"

    # 5. Markets config
    from pathlib import Path
    import json
    try:
        with open(settings.btc_markets_config_path) as f:
            markets = json.load(f)
        checks["markets_configured"] = len(markets) > 0
        checks["n_markets"] = len(markets)
    except Exception:
        checks["markets_configured"] = False
        checks["n_markets"] = 0

    # Overall readiness
    checks["ready_for_live"] = all([
        checks.get("py_clob_client_installed"),
        checks.get("polygon_usdc_sufficient"),
        checks.get("clob_api_key_set"),
        checks.get("wallet_address_set"),
        checks.get("markets_configured"),
    ])

    return checks
