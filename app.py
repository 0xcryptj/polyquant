"""
PolyQuant — single canonical entrypoint.

Usage:
    python app.py
    python app.py --no-telegram
    python app.py --no-web
    python app.py --log-json        # structured JSON logs (production)
    python app.py --log-level DEBUG

Docker CMD uses:
    python app.py --no-web --log-json
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Ensure project root is on sys.path when run directly
sys.path.insert(0, str(Path(__file__).resolve().parent))


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="PolyQuant trading bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Provider examples:
  python app.py                                       # paper trading defaults
  python app.py --wallet-provider=sdk                 # Phase 2: live CLOB wallet
  python app.py --wallet-provider=agentic             # Phase 3: awal CLI wallet
  python app.py --execution-provider=cli              # Phase 4: CLI execution
  python app.py --no-telegram --no-web --log-json     # headless production
""",
    )
    p.add_argument("--no-telegram", action="store_true",
                   help="Disable Telegram interface")
    p.add_argument("--no-web", action="store_true",
                   help="Disable web dashboard")
    p.add_argument("--log-level", default="INFO",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    p.add_argument("--log-json", action="store_true",
                   help="Output structured JSON logs (recommended in production)")
    p.add_argument("--wallet-provider",
                   choices=["sdk", "agentic", "awal", "mock", "none"], default=None,
                   help="Wallet backend (overrides WALLET_PROVIDER in .env)")
    p.add_argument("--execution-provider",
                   choices=["clob", "cli"], default=None,
                   help="Execution backend (overrides EXECUTION_PROVIDER in .env)")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    # Configure logging before anything else touches the log system
    from runtime.logging_setup import setup_logging
    setup_logging(level=args.log_level, json_output=args.log_json)

    import structlog
    log = structlog.get_logger("app")

    # Fail-fast: validate required env vars before spawning any service
    from config.settings import validate_startup
    try:
        validate_startup(require_telegram=not args.no_telegram)
    except SystemExit:
        raise
    except Exception as exc:
        log.critical("config_invalid", error=str(exc))
        sys.exit(1)

    from runtime.orchestrator import Orchestrator
    orch = Orchestrator(
        enable_telegram    = not args.no_telegram,
        enable_web         = not args.no_web,
        wallet_provider    = args.wallet_provider,
        execution_provider = args.execution_provider,
    )

    try:
        asyncio.run(orch.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
