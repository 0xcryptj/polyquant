"""
Funding flow utilities — check balances and USDC status on Polygon.
"""

from __future__ import annotations

import logging
from typing import Any

from config.settings import settings

logger = logging.getLogger(__name__)

USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
USDC_DECIMALS = 6

ERC20_ABI = [
    {"constant": True, "inputs": [{"name": "_owner", "type": "address"}],
     "name": "balanceOf", "outputs": [{"name": "balance", "type": "uint256"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "decimals",
     "outputs": [{"name": "", "type": "uint8"}], "type": "function"},
]


def get_balances(wallet_address: str) -> dict[str, float]:
    """
    Get MATIC and USDC balances for a wallet on Polygon.

    Returns:
        dict with keys: matic, usdc
    """
    from web3 import Web3

    w3 = Web3(Web3.HTTPProvider(settings.polygon_rpc_url))
    addr = Web3.to_checksum_address(wallet_address)

    matic_wei = w3.eth.get_balance(addr)
    matic = float(w3.from_wei(matic_wei, "ether"))

    usdc_contract = w3.eth.contract(
        address=Web3.to_checksum_address(USDC_ADDRESS),
        abi=ERC20_ABI,
    )
    usdc_raw = usdc_contract.functions.balanceOf(addr).call()
    usdc = usdc_raw / 10**USDC_DECIMALS

    return {"matic": matic, "usdc": usdc}


def print_funding_status(wallet_address: str) -> None:
    """Print a human-readable funding status report."""
    try:
        balances = get_balances(wallet_address)
        matic = balances["matic"]
        usdc = balances["usdc"]

        print(f"\n  Wallet:  {wallet_address}")
        print(f"  MATIC:   {matic:.4f} MATIC", end="")
        if matic < 0.01:
            print("  ⚠️  LOW — need ~0.01 MATIC for gas")
        else:
            print("  ✓")

        print(f"  USDC:    {usdc:.2f} USDC", end="")
        if usdc < 10:
            print("  ⚠️  LOW — need USDC to trade")
        else:
            print("  ✓")

    except Exception as exc:
        print(f"  ERROR fetching balances: {exc}")
        print("  Check your POLYGON_RPC_URL and wallet address in .env")
