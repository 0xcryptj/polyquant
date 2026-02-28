"""
One-time USDC + CTF contract approval for Polymarket trading.

Polymarket requires two ERC-20 approvals before trading:
1. USDC approval for the Exchange contract (collateral deposits)
2. CTF (Conditional Token Framework) approval for the Exchange contract

These approvals are permanent (max uint256) and only need to be set once
per wallet. The setup_wallet.py script calls this module.
"""

from __future__ import annotations

import logging
from typing import Any

from config.settings import settings

logger = logging.getLogger(__name__)

# Polygon contract addresses
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"  # USDC.e (PoS bridge)
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"   # CTF Exchange
EXCHANGE_ADDRESS = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"  # Polymarket Exchange

# ABI fragments
ERC20_APPROVE_ABI = [
    {
        "constant": False,
        "inputs": [
            {"name": "_spender", "type": "address"},
            {"name": "_value", "type": "uint256"},
        ],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [
            {"name": "_owner", "type": "address"},
            {"name": "_spender", "type": "address"},
        ],
        "name": "allowance",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function",
    },
]

CTF_APPROVAL_ABI = [
    {
        "constant": False,
        "inputs": [
            {"name": "operator", "type": "address"},
            {"name": "approved", "type": "bool"},
        ],
        "name": "setApprovalForAll",
        "outputs": [],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [
            {"name": "account", "type": "address"},
            {"name": "operator", "type": "address"},
        ],
        "name": "isApprovedForAll",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function",
    },
]

MAX_UINT256 = 2**256 - 1


def check_allowances(wallet_address: str) -> dict[str, bool]:
    """
    Check current approval status without sending any transactions.

    Returns:
        dict with keys: usdc_approved, ctf_approved
    """
    try:
        from web3 import Web3

        w3 = Web3(Web3.HTTPProvider(settings.polygon_rpc_url))
        addr = Web3.to_checksum_address(wallet_address)
        exchange = Web3.to_checksum_address(EXCHANGE_ADDRESS)

        usdc = w3.eth.contract(address=Web3.to_checksum_address(USDC_ADDRESS), abi=ERC20_APPROVE_ABI)
        allowance = usdc.functions.allowance(addr, exchange).call()
        usdc_approved = allowance > 10**18  # effectively unlimited

        ctf = w3.eth.contract(address=Web3.to_checksum_address(CTF_ADDRESS), abi=CTF_APPROVAL_ABI)
        ctf_approved = ctf.functions.isApprovedForAll(addr, exchange).call()

        return {"usdc_approved": usdc_approved, "ctf_approved": ctf_approved}

    except Exception as exc:
        logger.error("Allowance check failed: %s", exc)
        return {"usdc_approved": False, "ctf_approved": False}


def approve_usdc(private_key: str) -> str:
    """
    Approve the Polymarket Exchange contract to spend USDC.

    Args:
        private_key: Wallet private key (hex string, handled as secret by caller)

    Returns:
        Transaction hash (hex string)
    """
    from web3 import Web3

    w3 = Web3(Web3.HTTPProvider(settings.polygon_rpc_url))
    account = w3.eth.account.from_key(private_key)
    addr = account.address

    usdc = w3.eth.contract(address=Web3.to_checksum_address(USDC_ADDRESS), abi=ERC20_APPROVE_ABI)
    exchange = Web3.to_checksum_address(EXCHANGE_ADDRESS)

    tx = usdc.functions.approve(exchange, MAX_UINT256).build_transaction({
        "from": addr,
        "nonce": w3.eth.get_transaction_count(addr),
        "gas": 100_000,
        "maxFeePerGas": w3.eth.gas_price * 2,
        "maxPriorityFeePerGas": w3.eth.gas_price,
        "chainId": settings.POLYGON_CHAIN_ID,
    })

    signed = w3.eth.account.sign_transaction(tx, private_key)
    tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

    if receipt["status"] != 1:
        raise RuntimeError(f"USDC approve transaction failed: {tx_hash.hex()}")

    logger.info("USDC approved: tx=%s", tx_hash.hex())
    return tx_hash.hex()


def approve_ctf(private_key: str) -> str:
    """
    Approve the Polymarket Exchange contract as a CTF operator.

    Args:
        private_key: Wallet private key

    Returns:
        Transaction hash (hex string)
    """
    from web3 import Web3

    w3 = Web3(Web3.HTTPProvider(settings.polygon_rpc_url))
    account = w3.eth.account.from_key(private_key)
    addr = account.address

    ctf = w3.eth.contract(address=Web3.to_checksum_address(CTF_ADDRESS), abi=CTF_APPROVAL_ABI)
    exchange = Web3.to_checksum_address(EXCHANGE_ADDRESS)

    tx = ctf.functions.setApprovalForAll(exchange, True).build_transaction({
        "from": addr,
        "nonce": w3.eth.get_transaction_count(addr),
        "gas": 100_000,
        "maxFeePerGas": w3.eth.gas_price * 2,
        "maxPriorityFeePerGas": w3.eth.gas_price,
        "chainId": settings.POLYGON_CHAIN_ID,
    })

    signed = w3.eth.account.sign_transaction(tx, private_key)
    tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

    if receipt["status"] != 1:
        raise RuntimeError(f"CTF approval transaction failed: {tx_hash.hex()}")

    logger.info("CTF approved: tx=%s", tx_hash.hex())
    return tx_hash.hex()


def run_approvals(private_key: str, skip_if_approved: bool = True) -> dict[str, str | bool]:
    """
    Run both USDC and CTF approvals if not already set.

    Args:
        private_key:      Wallet private key
        skip_if_approved: If True, skip approval if already set

    Returns:
        Status dict with transaction hashes
    """
    from web3 import Web3
    wallet_address = Web3.eth.account.from_key(private_key).address

    results: dict[str, str | bool] = {}
    current = check_allowances(wallet_address)

    # USDC approval
    if current["usdc_approved"] and skip_if_approved:
        logger.info("USDC already approved — skipping")
        results["usdc_tx"] = "skipped"
        results["usdc_ok"] = True
    else:
        try:
            tx = approve_usdc(private_key)
            results["usdc_tx"] = tx
            results["usdc_ok"] = True
        except Exception as exc:
            logger.error("USDC approval failed: %s", exc)
            results["usdc_ok"] = False
            results["usdc_error"] = str(exc)

    # CTF approval
    if current["ctf_approved"] and skip_if_approved:
        logger.info("CTF already approved — skipping")
        results["ctf_tx"] = "skipped"
        results["ctf_ok"] = True
    else:
        try:
            tx = approve_ctf(private_key)
            results["ctf_tx"] = tx
            results["ctf_ok"] = True
        except Exception as exc:
            logger.error("CTF approval failed: %s", exc)
            results["ctf_ok"] = False
            results["ctf_error"] = str(exc)

    return results
