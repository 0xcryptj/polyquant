"""
CoinbaseAgenticProvider — wallet provider backed by the awal CLI.

The `awal` (AgentKit Wallet) CLI from Coinbase Developer Platform is invoked
via `npx awal@latest <command>`.  This provider wraps every call with:
  - configurable timeout   (AWAL_CMD_TIMEOUT seconds, default 30)
  - automatic JSON parsing (--json flag)
  - text fallback if JSON parse fails
  - status cache           (10-second TTL to avoid hammering)
  - Base → Polygon bridge  via `awal wallet bridge`

BOOTSTRAP (one-time, interactive — run BEFORE starting the bot):
    python scripts/awal_bootstrap.py

The bootstrap stores an auth session at AWAL_SESSION_FILE
(default: ~/.polyquant/awal_session.json).  The main bot reads this file
via the AWAL_SESSION_FILE env var injected into every subprocess.

NEVER call bootstrap() from inside the trading loop.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from config.settings import settings
from wallets.providers.base import WalletProvider, WalletStatus

logger = logging.getLogger(__name__)

# How long (seconds) a status() result is reused before re-querying
_STATUS_TTL = 10


class CoinbaseAgenticProvider(WalletProvider):
    """
    Wallet provider that delegates to the `awal` CLI via subprocess.

    All operations are safe to call from a thread pool.
    Each subprocess invocation respects AWAL_CMD_TIMEOUT.
    """

    name = "agentic"

    def __init__(
        self,
        session_file: str | Path | None = None,
        timeout: int | None = None,
    ) -> None:
        raw_path = session_file or settings.awal_session_file
        self._session_file = Path(raw_path).expanduser()
        self._timeout      = timeout or settings.awal_cmd_timeout

        # Status cache
        self._status_cache:    WalletStatus | None = None
        self._status_cache_ts: float               = 0.0

    # ── Low-level CLI runner ───────────────────────────────────────────────────

    def _run(self, *args: str, timeout: int | None = None) -> dict[str, Any]:
        """
        Execute:  npx awal@latest <args...> --json

        Returns:
            Parsed JSON dict on success.

        Raises:
            RuntimeError: if process exits non-zero.
            subprocess.TimeoutExpired: if command takes too long.
        """
        cmd = ["npx", "awal@latest", *args]
        if "--json" not in args:
            cmd.append("--json")

        env = {**os.environ}
        if self._session_file.exists():
            env["AWAL_SESSION_FILE"] = str(self._session_file)

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout or self._timeout,
            env=env,
        )

        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            raise RuntimeError(
                f"awal {args[0] if args else '?'} exited {result.returncode}: {stderr}"
            )

        stdout = (result.stdout or "").strip()
        if stdout:
            try:
                return json.loads(stdout)
            except json.JSONDecodeError:
                # Return raw text in a normalised envelope
                return {"ok": True, "raw": stdout}
        return {"ok": True}

    def _run_safe(self, *args: str) -> dict[str, Any] | None:
        """Like _run() but returns None instead of raising on any error."""
        try:
            return self._run(*args)
        except subprocess.TimeoutExpired:
            logger.warning("awal %s timed out after %ss", args[0] if args else "?", self._timeout)
        except RuntimeError as exc:
            logger.warning("awal command failed: %s", exc)
        except FileNotFoundError:
            logger.error(
                "npx not found — make sure Node.js ≥18 is installed. "
                "Run: node --version"
            )
        except Exception as exc:
            logger.warning("awal unexpected error: %s", exc)
        return None

    # ── WalletProvider interface ───────────────────────────────────────────────

    def address(self) -> str:
        """Always read from settings — awal uses the same key pair."""
        return settings.wallet_address

    def balance(self, chain: str = "polygon") -> float:
        """
        Query: npx awal@latest wallet balance --chain <chain> --json

        Falls back to agentkit_base web3 lookup on CLI failure.
        """
        data = self._run_safe("wallet", "balance", "--chain", chain)
        if data:
            # Accept multiple response shapes from different awal versions
            for key in ("usdc", "balance", "amount", "value"):
                if key in data:
                    try:
                        return float(data[key])
                    except (TypeError, ValueError):
                        pass

        # Graceful fallback: direct web3 RPC
        try:
            from wallets.agentkit_base import get_usdc_balance
            return get_usdc_balance(chain)
        except Exception as exc:
            logger.warning("balance fallback for %s failed: %s", chain, exc)
            return 0.0

    def ensure_funded(self, min_usdc: float, chain: str = "polygon") -> bool:
        """
        Ensure `min_usdc` USDC is available on `chain`.

        If the chain balance is insufficient and Base has enough,
        attempts a bridge:  npx awal@latest wallet bridge --amount X --from base --to polygon
        """
        current = self.balance(chain)
        if current >= min_usdc:
            return True

        if chain == "polygon":
            needed      = min_usdc - current
            base_balance = self.balance("base")
            if base_balance >= needed:
                logger.info(
                    "ensure_funded: bridging %.2f USDC Base→Polygon (have %.2f on Base)",
                    needed, base_balance,
                )
                return self._bridge(needed)

        logger.warning(
            "ensure_funded: need %.2f USDC on %s — have %.2f. "
            "Manual funding required.",
            min_usdc, chain, current,
        )
        return False

    def health(self) -> bool:
        """Quick liveness check: npx awal@latest wallet status --json"""
        data = self._run_safe("wallet", "status")
        if data is None:
            return False
        # awal status typically returns {"connected": true, ...}
        return data.get("connected", True)

    def status(self) -> WalletStatus:
        now = time.monotonic()
        if self._status_cache and (now - self._status_cache_ts) < _STATUS_TTL:
            return self._status_cache

        data    = self._run_safe("wallet", "status")
        ok      = data is not None and data.get("connected", True)
        addr    = self.address()
        poly    = self.balance("polygon")
        base    = self.balance("base")

        result = WalletStatus(
            healthy  = ok,
            address  = addr,
            provider = self.name,
            balances = {"polygon": poly, "base": base},
            details  = data or {"error": "awal status unavailable"},
        )
        self._status_cache    = result
        self._status_cache_ts = now
        return result

    def send(self, to: str, amount_usdc: float, chain: str = "polygon") -> str:
        """
        npx awal@latest wallet send --to <to> --amount <n> --asset USDC --chain <chain>

        Returns transaction hash.
        Raises RuntimeError on failure.
        """
        data = self._run(
            "wallet", "send",
            "--to",     to,
            "--amount", str(amount_usdc),
            "--asset",  "USDC",
            "--chain",  chain,
            timeout=60,  # sends may take longer
        )
        tx = data.get("txHash") or data.get("transactionHash") or data.get("tx") or ""
        if not tx:
            raise RuntimeError(f"awal send returned no txHash: {data}")
        logger.info("Sent %.2f USDC to %s on %s — tx=%s", amount_usdc, to, chain, tx)
        return tx

    # ── Private ───────────────────────────────────────────────────────────────

    def _bridge(self, amount_usdc: float) -> bool:
        """npx awal@latest wallet bridge --amount X --from base --to polygon"""
        data = self._run_safe(
            "wallet", "bridge",
            "--amount", str(amount_usdc),
            "--from",   "base",
            "--to",     "polygon",
        )
        if data is None:
            return False
        ok = data.get("ok", True)  # assume ok if key not present
        if ok:
            logger.info("Bridge initiated: %.2f USDC Base→Polygon", amount_usdc)
            # Invalidate status cache after balance change
            self._status_cache = None
        return bool(ok)

    # ── Bootstrap (one-time, interactive) ─────────────────────────────────────

    @classmethod
    def bootstrap(
        cls,
        session_file: str | Path | None = None,
    ) -> None:
        """
        Run the interactive awal auth flow to obtain and store a session token.

        Call this ONCE before starting the bot:
            python scripts/awal_bootstrap.py

        The session is saved to AWAL_SESSION_FILE and automatically injected
        into every subsequent subprocess invocation.
        """
        raw    = session_file or settings.awal_session_file
        path   = Path(raw).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)

        env = {**os.environ, "AWAL_SESSION_FILE": str(path)}

        print(f"\n{'='*60}")
        print("  Coinbase Agentic Wallet — Bootstrap Auth")
        print(f"  Session will be saved to: {path}")
        print(f"{'='*60}\n")
        print("Follow the prompts to complete OTP authentication.\n")

        result = subprocess.run(
            ["npx", "awal@latest", "auth", "login"],
            env=env,
        )

        if result.returncode == 0:
            print(f"\n✓ Bootstrap complete — session stored at {path}")
            print("  You can now start the bot: python app.py --wallet-provider=agentic\n")
        else:
            raise RuntimeError(
                "awal auth login failed. "
                "Check your Node.js installation and network connectivity."
            )
