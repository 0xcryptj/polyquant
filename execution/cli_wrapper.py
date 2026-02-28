"""
polymarket-cli subprocess wrapper.

Wraps the Node.js polymarket-cli tool as Python subprocess calls,
parsing JSON output for programmatic use.

Usage in code:
    from execution.cli_wrapper import run_cli, search_markets, get_clob_ok
"""

from __future__ import annotations

import json
import logging
import subprocess
import shutil
from typing import Any

logger = logging.getLogger(__name__)

CLI_COMMAND = "polymarket"  # assumes `npm install -g @polymarket/cli` or local path


def _check_cli_installed() -> bool:
    """Check if polymarket-cli is available in PATH."""
    return shutil.which(CLI_COMMAND) is not None


def run_cli(
    args: list[str],
    timeout: int = 30,
    json_output: bool = True,
) -> dict | list | str:
    """
    Run a polymarket-cli command and return parsed output.

    Args:
        args:        CLI arguments (e.g. ["markets", "search", "bitcoin 5 minute"])
        timeout:     Subprocess timeout in seconds
        json_output: If True, parse stdout as JSON

    Returns:
        Parsed JSON (dict/list) or raw string

    Raises:
        RuntimeError: If CLI is not installed or command fails
        json.JSONDecodeError: If JSON parsing fails
    """
    if not _check_cli_installed():
        raise RuntimeError(
            f"polymarket-cli not found. Install with:\n"
            f"  npm install -g @polymarket/cli\n"
            f"  or: npx @polymarket/cli {' '.join(args)}"
        )

    cmd = [CLI_COMMAND] + (args + ["-o", "json"] if json_output else args)

    logger.debug("Running: %s", " ".join(cmd))

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"polymarket-cli timed out after {timeout}s: {' '.join(args)}")

    if result.returncode != 0:
        raise RuntimeError(
            f"polymarket-cli failed (exit {result.returncode}):\n"
            f"  Command: {' '.join(cmd)}\n"
            f"  stderr: {result.stderr.strip()}"
        )

    if not json_output:
        return result.stdout.strip()

    # Some CLI commands wrap output in a top-level object; try to extract data
    stdout = result.stdout.strip()
    if not stdout:
        return {}

    return json.loads(stdout)


def search_markets(query: str) -> list[dict]:
    """
    Search for Polymarket markets matching a query string.

    Returns:
        List of market dicts from the CLI
    """
    result = run_cli(["markets", "search", query])
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        return result.get("markets", result.get("data", [result]))
    return []


def get_market(condition_id: str) -> dict:
    """Fetch a specific market by condition ID."""
    result = run_cli(["markets", "get", condition_id])
    if isinstance(result, dict):
        return result
    return {}


def get_clob_ok() -> bool:
    """
    Check CLOB API health.

    Returns:
        True if CLOB is healthy, False otherwise.
    """
    try:
        result = run_cli(["clob", "ok"], json_output=False)
        return "ok" in str(result).lower() or "true" in str(result).lower()
    except Exception as exc:
        logger.error("CLOB health check via CLI failed: %s", exc)
        return False


def approve_contracts() -> bool:
    """
    Run `polymarket approve set` to approve USDC + CTF contracts.

    This is the CLI-based alternative to wallets/allowance_setup.py.
    Both achieve the same result; use whichever is more convenient.

    Returns:
        True if approval succeeded.
    """
    try:
        result = run_cli(["approve", "set"], json_output=False, timeout=120)
        logger.info("Contract approvals set via CLI: %s", result)
        return True
    except Exception as exc:
        logger.error("Contract approval via CLI failed: %s", exc)
        return False


def get_order_book_cli(token_id: str) -> dict[str, Any]:
    """Fetch order book for a token via CLI."""
    try:
        return run_cli(["clob", "book", "--token-id", token_id])
    except Exception as exc:
        logger.error("CLI order book fetch failed for %s: %s", token_id[:12], exc)
        return {}
