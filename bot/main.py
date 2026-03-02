"""
Entry point — PolyQuant paper trading Telegram bot.

Usage:
    python bot/main.py

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
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


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
    has_markets, market_status = _check_markets()
    engine  = PaperEngine(starting_balance=STARTING_PAPER_BALANCE)
    model_status = _check_model(engine)
    logger.info("Markets: %s | Model: %s", market_status, model_status)
    learner = Learner()
    bot     = TelegramBot(engine=engine, learner=learner)

    # ── Queue startup notification ────────────────────────────────────────
    # The job queue sends it ~5 s after polling starts so the bot is ready
    async def _startup_notification(ctx) -> None:
        s = engine.get_status()
        market_icon = "📡" if has_markets else "🔬"
        mode_str = "Real Markets" if has_markets else "Simulation"

        text = (
            f"🚀 *PolyQuant Bot Started*\n\n"
            f"_Paper-trading simulations. Claude learns from results._\n\n"
            f"💰 Bankroll: ${s['balance']:.2f}\n"
            f"{market_icon} {mode_str}\n"
            f"  {market_status}\n"
            f"🧠 {model_status}\n\n"
            f"📋 Trades: {s['n_total']} · {s['n_wins']} wins / {s['n_losses']} losses\n"
            f"   Open: {s['n_open']} · Total profit: ${s['total_pnl']:+.2f}\n\n"
            f"⚙️ Loop: every 60 s · Trades close in ~5 min\n\n"
            f"Tip: Run only *one* bot instance to avoid balance mix-ups."
        )
        await bot._notify(text, reply_markup=MAIN_KEYBOARD)

    bot.app.job_queue.run_once(_startup_notification, when=5)

    # ── Run ───────────────────────────────────────────────────────────────
    logger.info("Starting Telegram bot (chat=%s)...", settings.telegram_chat_id)
    try:
        bot.run()
    except KeyboardInterrupt:
        logger.info("Bot stopped.")
    except Exception as exc:
        logger.critical("Bot crashed: %s", exc, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
