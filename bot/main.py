"""
Entry point — PolyQuant paper trading Telegram bot.

Usage:
    python bot/main.py

Trading and data refresh start immediately on launch.

Quick-start guide:
  1. Copy .env.example → .env and fill in:
       TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (required for bot)
       BINANCE_BASE_URL (defaults to https://api.binance.com)
  2. (Optional) Run market discovery:
       python scripts/find_btc_markets.py
  3. Start the bot:
       python bot/main.py

The bot works in SIMULATION MODE when no markets are configured —
it generates synthetic Polymarket-style bets from BTC price signals
so the learning loop can run immediately.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _kill_existing_instance(pid_file: Path, logger: "logging.Logger") -> None:
    """
    If another bot process is running (tracked by pid_file), kill it before
    we start so there's no Telegram 409 Conflict.  Safe to call even if no
    previous instance exists.
    """
    if not pid_file.exists():
        return
    try:
        old_pid = int(pid_file.read_text().strip())
    except Exception:
        pid_file.unlink(missing_ok=True)
        return
    if old_pid == os.getpid():
        return  # Same process — shouldn't happen, but guard anyway

    alive = False
    try:
        if sys.platform == "win32":
            import subprocess as _sp
            r = _sp.run(
                ["tasklist", "/FI", f"PID eq {old_pid}", "/FO", "CSV"],
                capture_output=True, text=True, timeout=5,
            )
            alive = str(old_pid) in r.stdout
        else:
            os.kill(old_pid, 0)  # signal 0 = existence check only
            alive = True
    except (ProcessLookupError, PermissionError, OSError):
        alive = False

    if alive:
        logger.warning(
            "Found existing bot instance (PID %d) — killing it to avoid Telegram 409 Conflicts...",
            old_pid,
        )
        try:
            if sys.platform == "win32":
                import subprocess as _sp
                _sp.run(["taskkill", "/F", "/PID", str(old_pid)], capture_output=True, timeout=5)
            else:
                import signal as _signal
                os.kill(old_pid, _signal.SIGTERM)
            time.sleep(3)  # Let the old process fully exit
            logger.info("Old instance terminated. Starting fresh.")
        except Exception as exc:
            logger.warning("Could not kill old instance (pid=%d): %s. Continuing anyway.", old_pid, exc)
    else:
        logger.debug("Stale PID file (pid=%d no longer running). Ignoring.", old_pid)

    pid_file.unlink(missing_ok=True)


def _setup_logging() -> None:
    from config.settings import PROJECT_ROOT
    log_dir = PROJECT_ROOT / "paper_trading"
    log_dir.mkdir(exist_ok=True)
    # Use UTF-8 for stdout to avoid encoding errors on Windows (cp1252 default)
    stdout_handler = logging.StreamHandler(sys.stdout)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass  # Python < 3.7 or non-reconfigurable stream
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            stdout_handler,
            logging.FileHandler(str(log_dir / "bot.log"), encoding="utf-8"),
        ],
    )
    for noisy in ("httpx", "httpcore", "ccxt", "telegram", "apscheduler", "hpack"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _check_markets() -> tuple[bool, str]:
    """Return (has_markets, status_message)."""
    import json
    from config.settings import settings
    path = settings.btc_markets_config_path
    try:
        with open(path) as f:
            markets = json.load(f)
        if markets:
            return True, f"{len(markets)} Polymarket BTC markets loaded"
        return False, "SIMULATION MODE (markets config empty — run scripts/find_btc_markets.py)"
    except FileNotFoundError:
        return False, f"SIMULATION MODE (no {path})"
    except Exception as e:
        return False, f"SIMULATION MODE (config error: {e})"


def _check_model(engine: "PaperEngine" | None = None) -> str:
    """Return strategy description: base model + Claude LLM if available."""
    from config.settings import PROJECT_ROOT
    base = "Calibration model" if (PROJECT_ROOT / "models/saved/calibration_model.joblib").exists() else "Momentum heuristic"
    llm_ok = engine and engine._llm and engine._llm.is_available
    if llm_ok:
        return f"{base} + Claude LLM (learns & refines)"
    return f"{base} (set ANTHROPIC_API_KEY for Claude)"


def main() -> None:
    _setup_logging()
    logger = logging.getLogger(__name__)
    try:
        from config.settings import settings
        from paper_trading.engine import PaperEngine, STARTING_PAPER_BALANCE
        from paper_trading.learner import Learner
        from bot.telegram_bot import MAIN_KEYBOARD, TelegramBot
    except Exception as exc:
        logger.critical("Failed to load settings — check your .env file.\nError: %s", exc)
        sys.exit(1)

    logger.info("Settings loaded: %s", settings.safe_summary())

    # ── Instance lock: kill any previous bot process to avoid Telegram 409s ──
    from config.settings import PROJECT_ROOT
    pid_file = PROJECT_ROOT / "paper_trading" / "bot.pid"
    _kill_existing_instance(pid_file, logger)
    pid_file.write_text(str(os.getpid()))

    has_markets, market_status = _check_markets()
    engine  = PaperEngine(starting_balance=STARTING_PAPER_BALANCE)
    model_status = _check_model(engine)
    logger.info("Markets: %s | Model: %s", market_status, model_status)
    learner = Learner()
    bot     = TelegramBot(engine=engine, learner=learner)

    logger.info(
        "Starting PolyQuant bot (chat=%s) — trading starts automatically.",
        settings.telegram_chat_id,
    )
    try:
        bot.run()
    except KeyboardInterrupt:
        logger.info("Bot stopped.")
    except Exception as exc:
        logger.critical("Bot crashed: %s", exc, exc_info=True)
        sys.exit(1)
    finally:
        pid_file.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
