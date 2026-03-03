"""
CoinbaseAWALProvider — wallet provider backed by the `awal` CLI (WSL-friendly).

Uses AWAL_BIN (default: awal) — works in WSL. Session via AWAL_SESSION_FILE.

Commands:
  awal status          (or awal wallet status)
  awal address
  awal balance         (or awal wallet balance --chain X)

Placeholders for send()/trade() — not fully used yet.
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

_STATUS_TTL = 10


class CoinbaseAWALProvider(WalletProvider):
    """
    Wallet provider that shells out to `awal` (configurable via AWAL_BIN).

    Designed for WSL. Uses subprocess with timeouts.
    """

    name = "awal"

    def __init__(
        self,
        bin_path: str | None = None,
        session_file: str | Path | None = None,
        timeout: int | None = None,
    ) -> None:
        self._bin = (bin_path or getattr(settings, "awal_bin", None) or "awal").strip()
        raw_path = session_file or getattr(settings, "awal_session_file", "~/.polyquant/awal_session.json")
        self._session_file = Path(raw_path).expanduser()
        self._timeout = timeout or getattr(settings, "awal_cmd_timeout", 30)

        self._status_cache: WalletStatus | None = None
        self._status_cache_ts: float = 0.0

    def _run(self, *args: str, timeout: int | None = None) -> dict[str, Any]:
        cmd = [self._bin, *args]
        if "--json" not in args:
            cmd.append("--json")

        env = dict(os.environ)
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
            raise RuntimeError(f"awal exited {result.returncode}: {stderr}")

        out = (result.stdout or "").strip()
        if out:
            try:
                return json.loads(out)
            except json.JSONDecodeError:
                return {"ok": True, "raw": out}
        return {"ok": True}

    def _run_safe(self, *args: str, timeout: int | None = None) -> dict[str, Any] | None:
        try:
            return self._run(*args, timeout=timeout)
        except (subprocess.TimeoutExpired, FileNotFoundError, RuntimeError) as exc:
            logger.debug("awal %s failed: %s", args[0] if args else "?", exc)
        except Exception as exc:
            logger.warning("awal unexpected error: %s", exc)
        return None

    def health(self) -> bool:
        """Quick liveness: awal status or awal wallet status."""
        data = self._run_safe("status", timeout=5)
        if data is None:
            data = self._run_safe("wallet", "status", timeout=5)
        return data is not None and data.get("connected", True)

    def address(self) -> str:
        """awal address or settings.wallet_address."""
        data = self._run_safe("address", timeout=5)
        if data:
            addr = data.get("address") or data.get("wallet") or data.get("walletAddress")
            if addr:
                return str(addr)
        return settings.wallet_address

    def balance(self, chain: str = "polygon") -> float:
        """awal balance or awal wallet balance --chain X."""
        data = self._run_safe("balance", "--chain", chain, timeout=10)
        if data is None:
            data = self._run_safe("wallet", "balance", "--chain", chain, timeout=10)
        if data:
            for key in ("usdc", "balance", "amount", "value"):
                if key in data:
                    try:
                        return float(data[key])
                    except (TypeError, ValueError):
                        pass
        try:
            from wallets.agentkit_base import get_usdc_balance
            return get_usdc_balance(chain)
        except Exception:
            return 0.0

    def status(self) -> WalletStatus:
        now = time.monotonic()
        if self._status_cache and (now - self._status_cache_ts) < _STATUS_TTL:
            return self._status_cache

        data = self._run_safe("wallet", "status") or self._run_safe("status")
        ok = data is not None and data.get("connected", True)
        addr = self.address()
        poly = self.balance("polygon")
        base = self.balance("base")

        result = WalletStatus(
            healthy=ok,
            address=addr,
            provider=self.name,
            balances={"polygon": poly, "base": base},
            details=data or {"error": "awal status unavailable"},
        )
        self._status_cache = result
        self._status_cache_ts = now
        return result

    def ensure_funded(self, min_usdc: float, chain: str = "polygon") -> bool:
        current = self.balance(chain)
        if current >= min_usdc:
            return True
        if chain == "polygon":
            needed = min_usdc - current
            base_bal = self.balance("base")
            if base_bal >= needed:
                data = self._run_safe(
                    "wallet", "bridge",
                    "--amount", str(needed),
                    "--from", "base",
                    "--to", "polygon",
                    timeout=60,
                )
                if data and data.get("ok", True):
                    self._status_cache = None
                    return True
        return False
