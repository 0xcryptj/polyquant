"""
AWAL Wallet Doctor — run: python -m wallets.doctor_awal

Prints diagnostics and instructions for WSL, WSLg, and missing libs.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def main() -> int:
    print("=" * 60)
    print("  AWAL Wallet Doctor")
    print("=" * 60)

    # 1. Check AWAL binary
    try:
        from config.settings import settings
        awal_bin = getattr(settings, "awal_bin", "awal") or "awal"
    except Exception:
        awal_bin = os.environ.get("AWAL_BIN", "awal")

    path = shutil.which(awal_bin)
    if path:
        print(f"\n[OK] {awal_bin} found: {path}")
    else:
        print(f"\n[FAIL] {awal_bin} not found in PATH")
        print("  Install: npm install -g @coinbase/awal-cli")
        print("  Or use: npx awal@latest")
        print("  Set AWAL_BIN=npx awal@latest in .env")

    # 2. Node.js (for npx)
    node = shutil.which("node")
    if node:
        try:
            r = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=2)
            print(f"\n[OK] Node.js: {r.stdout.strip()}")
        except Exception:
            print("\n[?] Node.js: found but --version failed")
    else:
        print("\n[FAIL] node not found - required for npx awal")
        print("  Install Node.js ≥18: https://nodejs.org/")

    # 3. Session file
    session_path = Path(os.environ.get("AWAL_SESSION_FILE", "~/.polyquant/awal_session.json")).expanduser()
    if session_path.exists():
        print(f"\n[OK] Session file: {session_path}")
    else:
        print(f"\n[FAIL] Session file not found: {session_path}")
        print("  Run: python scripts/awal_bootstrap.py")
        print("  Then authenticate with Coinbase")

    # 4. Status check
    try:
        from wallets.providers.coinbase_awal import CoinbaseAWALProvider
        p = CoinbaseAWALProvider()
        ok = p.health()
        if ok:
            addr = p.address()
            poly = p.balance("polygon")
            base = p.balance("base")
            print(f"\n[OK] AWAL healthy")
            print(f"  Address: {addr[:16]}...")
            print(f"  Polygon USDC: ${poly:.2f}")
            print(f"  Base USDC: ${base:.2f}")
        else:
            print("\n[FAIL] AWAL healthcheck failed")
    except Exception as e:
        print(f"\n[FAIL] Provider check failed: {e}")

    # 5. WSLg instructions
    print("\n" + "-" * 60)
    print("WSL Runtime:")
    print("  - Run the bot in WSL Ubuntu (wallet works there)")
    print("  - To open wallet GUI: awal show")
    print("  - WSLg required for GUI; install: wsl --update")
    print("  - Deposit Base USDC to your wallet address")
    print("-" * 60)

    return 0


if __name__ == "__main__":
    sys.exit(main())
