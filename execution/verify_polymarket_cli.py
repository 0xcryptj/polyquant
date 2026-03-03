"""
Verify Polymarket CLI — run: python -m execution.verify_polymarket_cli

Prints PASS or FAIL with diagnostics.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

# Ensure project root on path
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def main() -> int:
    # Load polymarket_cli_provider without pulling execution.clob_client (tenacity)
    spec = importlib.util.spec_from_file_location(
        "polymarket_cli_provider",
        _PROJECT_ROOT / "execution" / "providers" / "polymarket_cli_provider.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    cmd = mod._get_cli_cmd()
    print("Polymarket CLI verification")
    print("-" * 40)
    if not cmd:
        print("FAIL: POLYMARKET_CLI_CMD or POLYMARKET_CLI_PATH not set")
        print("  Set in .env or env, e.g. POLYMARKET_CLI_CMD=npx polymarket-cli")
        return 1

    print(f"  CLI: {cmd}")
    ok = mod.healthcheck()
    if not ok:
        print("FAIL: healthcheck() returned False")
        return 1

    print("  healthcheck: OK")
    markets = mod.list_markets()
    print(f"  list_markets: {len(markets)} market(s)")

    if markets:
        first = markets[0]
        tid = first.get("token_id") or first.get("condition_id") or ""
        if tid:
            ob = mod.get_orderbook(tid)
            if ob:
                print(f"  get_orderbook({tid[:12]}...): bid={ob.get('bid')} ask={ob.get('ask')}")
            else:
                print(f"  get_orderbook: failed for {tid[:12]}...")
    else:
        print("  get_orderbook: skipped (no markets)")

    print("-" * 40)
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
