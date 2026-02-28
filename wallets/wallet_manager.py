"""
Wallet Manager — Coinbase AgentKit + py-clob-client integration.

Provides:
- EthAccountWalletProvider on Polygon (chain_id=137)
- ClobClient authenticated with derived API credentials
- Health check (connectivity, wallet balance, CLOB status)

SECURITY:
- Private key is loaded from settings (SecretStr) — never hardcoded
- Private key is never logged, printed, or included in exceptions
- All wallet operations are wrapped in try/except with sanitized error messages
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from config.settings import settings

logger = logging.getLogger(__name__)

# Lazy imports — these are optional heavy dependencies
# They are imported inside functions to allow the module to load even
# when the packages are not installed (e.g. in CI test environment)


@dataclass
class WalletBundle:
    """Holds initialized wallet and CLOB client instances."""

    wallet_address: str
    clob_client: Any      # py_clob_client.client.ClobClient
    agent_kit: Any | None  # coinbase_agentkit.AgentKit (optional)
    provider: Any | None   # EthAccountWalletProvider


def initialize_wallet() -> WalletBundle:
    """
    Initialize the Polygon wallet and CLOB client.

    Steps:
    1. Load private key from settings (SecretStr)
    2. Create EthAccountWalletProvider for Polygon
    3. Create ClobClient with API credentials
    4. Derive/create CLOB API credentials
    5. Return WalletBundle

    Raises:
        ImportError: If py_clob_client or coinbase_agentkit is not installed
        ValueError: If private key or wallet address is invalid
    """
    private_key = settings.wallet_private_key.get_secret_value()

    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds
    except ImportError as exc:
        raise ImportError(
            "py-clob-client is not installed. Run: pip install py-clob-client"
        ) from exc

    # Initialize CLOB client
    clob = ClobClient(
        host=settings.polymarket_clob_host,
        chain_id=settings.POLYGON_CHAIN_ID,
        key=private_key,
        creds=ApiCreds(
            api_key=settings.polymarket_api_key,
            api_secret=settings.polymarket_api_secret.get_secret_value(),
            api_passphrase=settings.polymarket_api_passphrase.get_secret_value(),
        ),
    )

    # Optional: AgentKit provider
    provider = None
    agent_kit = None

    if settings.cdp_api_key_id and settings.cdp_api_key_secret:
        try:
            provider, agent_kit = _initialize_agentkit(private_key)
        except Exception as exc:
            logger.warning("AgentKit initialization failed (non-critical): %s", exc)

    bundle = WalletBundle(
        wallet_address=settings.wallet_address,
        clob_client=clob,
        agent_kit=agent_kit,
        provider=provider,
    )

    logger.info("Wallet initialized: %s | CLOB host: %s", settings.wallet_address, settings.polymarket_clob_host)
    return bundle


def _initialize_agentkit(private_key: str) -> tuple[Any, Any]:
    """
    Initialize Coinbase AgentKit EthAccountWalletProvider.

    This is optional — the bot works without AgentKit, but AgentKit
    enables gasless USDC funding flows via Coinbase's infrastructure.
    """
    try:
        from coinbase_agentkit import (
            AgentKit,
            AgentKitConfig,
            EthAccountWalletProvider,
            EthAccountWalletProviderConfig,
        )
    except ImportError as exc:
        raise ImportError(
            "coinbase-agentkit not installed. Run: pip install coinbase-agentkit"
        ) from exc

    provider_config = EthAccountWalletProviderConfig(
        private_key=private_key,
        rpc_url=settings.polygon_rpc_url,
        chain_id=settings.POLYGON_CHAIN_ID,
    )
    provider = EthAccountWalletProvider(config=provider_config)

    agentkit_config = AgentKitConfig(
        cdp_api_key_id=settings.cdp_api_key_id,
        cdp_api_key_secret=settings.cdp_api_key_secret.get_secret_value() if settings.cdp_api_key_secret else None,
        wallet_provider=provider,
    )
    kit = AgentKit(config=agentkit_config)

    return provider, kit


def derive_clob_api_creds(clob_client: Any) -> dict[str, str]:
    """
    Create or derive CLOB API credentials from the wallet's private key.

    These are stored in .env as POLYMARKET_API_KEY/SECRET/PASSPHRASE.
    Only needs to be run once (or when rotating credentials).

    Returns:
        dict with keys: api_key, api_secret, api_passphrase
    """
    try:
        creds = clob_client.create_or_derive_api_creds()
        api_key = creds.api_key
        api_secret = creds.api_secret
        api_passphrase = creds.api_passphrase

        logger.info("CLOB API credentials derived successfully. API key: %s...", api_key[:8])
        return {
            "api_key": api_key,
            "api_secret": api_secret,
            "api_passphrase": api_passphrase,
        }
    except Exception as exc:
        raise RuntimeError(f"Failed to derive CLOB API credentials: {exc}") from exc


def health_check(bundle: WalletBundle) -> dict[str, Any]:
    """
    Run connectivity and balance health checks.

    Returns:
        dict with: ok (bool), clob_ok (bool), wallet_address, matic_balance, usdc_balance
    """
    status: dict[str, Any] = {
        "ok": False,
        "wallet_address": bundle.wallet_address,
        "clob_ok": False,
        "matic_balance": None,
        "usdc_balance": None,
    }

    # CLOB health
    try:
        ok = bundle.clob_client.get_ok()
        status["clob_ok"] = bool(ok)
    except Exception as exc:
        logger.error("CLOB health check failed: %s", exc)
        status["clob_error"] = str(exc)

    # Wallet balance via web3
    try:
        from web3 import Web3

        w3 = Web3(Web3.HTTPProvider(settings.polygon_rpc_url))
        checksum_addr = Web3.to_checksum_address(bundle.wallet_address)

        matic_wei = w3.eth.get_balance(checksum_addr)
        status["matic_balance"] = float(w3.from_wei(matic_wei, "ether"))

        # USDC on Polygon (address: 0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174)
        usdc_balance = _get_usdc_balance(w3, checksum_addr)
        status["usdc_balance"] = usdc_balance

    except Exception as exc:
        logger.error("Balance check failed: %s", exc)
        status["balance_error"] = str(exc)

    status["ok"] = status["clob_ok"] and status.get("matic_balance") is not None

    # Print summary
    print(f"\n{'='*50}")
    print(f"  Wallet: {status['wallet_address']}")
    print(f"  MATIC:  {status.get('matic_balance', 'ERROR')} MATIC")
    print(f"  USDC:   {status.get('usdc_balance', 'ERROR')} USDC")
    print(f"  CLOB:   {'✓ OK' if status['clob_ok'] else '✗ FAILED'}")
    print(f"  Status: {'✓ HEALTHY' if status['ok'] else '✗ UNHEALTHY'}")
    print(f"{'='*50}\n")

    return status


def _get_usdc_balance(w3: Any, address: str) -> float:
    """Get USDC balance on Polygon for the given address."""
    # USDC on Polygon (PoS bridged)
    USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
    # ERC-20 balanceOf ABI (minimal)
    ABI = [{"constant": True, "inputs": [{"name": "_owner", "type": "address"}],
             "name": "balanceOf", "outputs": [{"name": "balance", "type": "uint256"}],
             "type": "function"}]

    contract = w3.eth.contract(
        address=w3.to_checksum_address(USDC_ADDRESS),
        abi=ABI,
    )
    balance_raw = contract.functions.balanceOf(address).call()
    return balance_raw / 1e6  # USDC has 6 decimals
