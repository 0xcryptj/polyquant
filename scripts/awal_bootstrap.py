"""
awal Bootstrap — one-time interactive auth for Coinbase Agentic Wallets.

Run this ONCE on the machine where the bot will run, BEFORE starting the bot:

    python scripts/awal_bootstrap.py

What it does:
  1. Checks that Node.js ≥18 and npx are available.
  2. Runs `npx awal@latest auth login` (interactive OTP flow).
  3. Verifies the session by calling `npx awal@latest wallet status`.
  4. Saves the session to AWAL_SESSION_FILE (default: ~/.polyquant/awal_session.json).

After bootstrapping:
    python app.py --wallet-provider=agentic

IMPORTANT:
  - Never run this from inside the main trading loop.
  - Never commit the session file to version control.
  - The session file contains sensitive tokens — restrict its permissions (chmod 600).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

# Ensure project root is on the path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _check_node() -> None:
    """Verify Node.js ≥18 is available."""
    try:
        result = subprocess.run(
            ["node", "--version"],
            capture_output=True, text=True, timeout=5,
        )
        version_str = result.stdout.strip()  # e.g. "v20.11.0"
        major = int(version_str.lstrip("v").split(".")[0])
        if major < 18:
            print(f"✗ Node.js {version_str} found but ≥18 required.")
            print("  Install from https://nodejs.org/ and retry.")
            sys.exit(1)
        print(f"✓ Node.js {version_str}")
    except FileNotFoundError:
        print("✗ node not found. Install Node.js ≥18 from https://nodejs.org/")
        sys.exit(1)


def _check_npx() -> None:
    """Verify npx is available."""
    try:
        subprocess.run(
            ["npx", "--version"],
            capture_output=True, text=True, timeout=5,
        )
        print("✓ npx available")
    except FileNotFoundError:
        print("✗ npx not found. It should ship with Node.js ≥5.2.")
        sys.exit(1)


def main() -> None:
    from config.settings import settings
    from wallets.providers.coinbase_agentic import CoinbaseAgenticProvider

    print("\n" + "=" * 60)
    print("  PolyQuant — Coinbase Agentic Wallet Bootstrap")
    print("=" * 60 + "\n")

    # Pre-flight checks
    _check_node()
    _check_npx()

    session_path = Path(settings.awal_session_file).expanduser()
    if session_path.exists():
        resp = input(
            f"\nSession file already exists at {session_path}.\n"
            "Re-authenticate? [y/N] "
        ).strip().lower()
        if resp != "y":
            print("Skipped. Using existing session.")
        else:
            CoinbaseAgenticProvider.bootstrap(session_file=session_path)
    else:
        CoinbaseAgenticProvider.bootstrap(session_file=session_path)

    # Restrict permissions on Unix (chmod 600)
    if sys.platform != "win32":
        try:
            session_path.chmod(0o600)
            print(f"  Session file permissions set to 600: {session_path}")
        except OSError as exc:
            print(f"  Warning: could not chmod session file: {exc}")

    # Verify the session works
    print("\nVerifying session with `awal wallet status`...")
    provider = CoinbaseAgenticProvider(session_file=session_path)
    status   = provider.status()

    if status.healthy:
        print(f"\n✓ Session verified!")
        print(f"  Address:  {status.address}")
        print(f"  Balances: {status.balances}")
        print(f"\nYou can now start the bot:")
        print(f"  python app.py --wallet-provider=agentic")
    else:
        print("\n✗ Session verification failed.")
        print(f"  Details: {status.details}")
        print("  Try running this script again.")
        sys.exit(1)


if __name__ == "__main__":
    main()
