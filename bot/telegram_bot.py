"""
Telegram Bot — PolyGun-style signal alerts + adaptive learning commands.

Start flow: Run `python bot/main.py`. Trading, sentiment refresh, wallet
updates, and the web dashboard all start automatically — no /start needed.
Use ⏸ Pause / ▶️ Resume buttons to stop and restart trading + the web server.

Performance design:
  - ALL blocking work (DB, HTTP, engine cycle) runs in asyncio.to_thread()
    so the event loop is never frozen — commands respond in <100ms
  - Sentiment is refreshed every 3 min in background; /sentiment reads cache
  - Wallet analysis is refreshed every 5 min in background; /wallets reads cache
  - Trading loop is fully offloaded to thread pool; it never touches event loop

Commands:
  /start       — greeting + current status (trading already running)
  /help        — command reference
  /status      — balance, PnL, open positions, loop state
  /positions   — open paper positions
  /trades [n]  — last n resolved trades (default 10)
  /performance — win rate, Sharpe, Brier, drawdown
  /learn       — adaptive learning report + parameter changes
  /params      — current trading parameters
  /sentiment   — Fear & Greed, funding rate, news (cached, instant)
  /wallets     — whale wallet analysis (cached, instant)
  /live_check  — readiness check for transitioning to live trading
  /reset       — reset paper balance (confirmation required)
  /webui       — start/stop/open Web dashboard (http://localhost:8080)
  /find_markets — discover BTC markets from Polymarket (saves to config)
  /start_bot   — resume trading loop + web server
  /stop_bot    — pause trading loop + stop web server
"""

from __future__ import annotations

import asyncio
import logging
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.error import TelegramError

from config.settings import settings
from paper_trading.engine import PaperEngine
from paper_trading.learner import Learner
from paper_trading import persistence as db
from paper_trading.utils import age_seconds, split_message

logger = logging.getLogger(__name__)

LOOP_INTERVAL = 25          # seconds between trading cycles (low latency)

# Reply keyboard: 4 rows × 3 buttons
KEYBOARD_ROWS = [
    [KeyboardButton("📊 Status"), KeyboardButton("📋 Positions"), KeyboardButton("📈 Trades")],
    [KeyboardButton("📉 Performance"), KeyboardButton("🌡 Sentiment"), KeyboardButton("🧠 Learn")],
    [KeyboardButton("▶️ Resume"), KeyboardButton("⏸ Pause"), KeyboardButton("🔄 Reset")],
    [KeyboardButton("🌐 Web UI"), KeyboardButton("🔧 Find Markets"), KeyboardButton("⚙ Params")],
]
MAIN_KEYBOARD = ReplyKeyboardMarkup(
    KEYBOARD_ROWS,
    resize_keyboard=True,
    input_field_placeholder="Tap a button or type /help",
)
SENTIMENT_TTL = 180         # seconds between sentiment refreshes (3 min — always fresh)
WALLET_TTL    = 300         # seconds between wallet analysis refreshes (5 min)

_trading_active = True
_web_ui_process = None  # subprocess.Popen for web server


def _port_free(port: int) -> bool:
    """Return True if nothing is bound to port on localhost."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


class TelegramBot:
    def __init__(self, engine: PaperEngine, learner: Learner) -> None:
        self.engine  = engine
        self.learner = learner
        self._chat_id = settings.telegram_chat_id
        token = settings.telegram_bot_token.get_secret_value()
        # updater(None) disables the built-in Updater so python-telegram-bot
        # does NOT start any internal polling — we do all polling ourselves.
        self.app = Application.builder().token(token).updater(None).build()

        # Guard: _start_background() is idempotent — called once on run()
        self._background_started = False

        # ── Caches (populated by background jobs) ─────────────────────────
        self._sentiment_cache: dict | None = None
        self._wallet_report_cache: str = "Wallet data loading — try again in 30 seconds."

        self._register_handlers()

    def run(self) -> None:
        """
        Start the bot using a manual async polling loop.
        Clears webhook first so long-polling works; bypasses run_polling()
        to avoid 409 Conflict when a previous session lingers.
          - Tracks the update offset correctly so we never re-process
          - On Conflict: waits 30 s and retries — usually resolves itself
          - On any other error: waits 5 s and retries
          - Runs the APScheduler job queue alongside the polling
        """
        import asyncio as _asyncio
        import httpx

        async def _polling_loop() -> None:
            def _clear_webhook() -> str:
                with httpx.Client(timeout=10.0) as c:
                    return c.get(
                        f"https://api.telegram.org/bot{settings.telegram_bot_token.get_secret_value()}/deleteWebhook"
                        "?drop_pending_updates=true"
                    ).text[:80]
            try:
                txt = await asyncio.to_thread(_clear_webhook)
                logger.info("Webhook cleared: %s", txt)
            except Exception as e:
                logger.warning("Webhook clear failed (non-critical): %s", e)

            offset = 0
            conflict_count = 0
            await self.app.initialize()
            await self.app.start()

            # Auto-start trading jobs and web server immediately — no /start needed
            try:
                await self._start_background()
                self._start_web_ui()
                logger.info("Bot polling started. Trading running. Web dashboard at http://localhost:8080")
            except Exception as exc:
                logger.error("Auto-start error (non-fatal, bot will still poll): %s", exc, exc_info=True)

            try:
                while True:
                    try:
                        # timeout=0 = short-poll: Telegram returns immediately with
                        # whatever updates are queued (no lingering server session).
                        # We sleep 1s between polls to avoid hammering the API.
                        updates = await self.app.bot.get_updates(
                            offset=offset,
                            timeout=0,
                            allowed_updates=["message", "callback_query"],
                        )
                        conflict_count = 0  # reset streak on success

                        for upd in updates:
                            await self.app.process_update(upd)
                            offset = upd.update_id + 1

                        await _asyncio.sleep(1)

                    except TelegramError as exc:
                        if "Conflict" in str(exc):
                            conflict_count += 1
                            # Competing sessions use long-polling (timeout≈30-60s).
                            # We must wait for their request to expire before our
                            # short-poll can succeed. Back off progressively up to
                            # 45s. If the PID lock in main.py killed the old process,
                            # conflicts should resolve quickly (usually 1-2 retries).
                            wait = min(5 * conflict_count, 45)
                            logger.warning(
                                "Telegram Conflict #%d — another session active, waiting %ds "
                                "(restart bot to kill old instance automatically)",
                                conflict_count, wait,
                            )
                            await _asyncio.sleep(wait)
                        elif "Timed out" in str(exc) or "timeout" in str(exc).lower():
                            await _asyncio.sleep(1)
                        else:
                            logger.error("Telegram error: %s", exc)
                            await _asyncio.sleep(5)
                    except _asyncio.CancelledError:
                        break
                    except Exception as exc:
                        logger.error("Polling error: %s", exc)
                        await _asyncio.sleep(5)
            finally:
                await self.app.stop()
                await self.app.shutdown()

        _asyncio.run(_polling_loop())

    async def _start_background(self) -> None:
        """Start trading loop, sentiment, wallet refresh, and daily summary. Called once when user sends /start."""
        if self._background_started:
            return
        self._background_started = True
        jq = self.app.job_queue
        jq.run_repeating(self._trading_loop_job, interval=LOOP_INTERVAL, first=10)
        jq.run_repeating(self._refresh_sentiment_job, interval=SENTIMENT_TTL, first=15)
        jq.run_repeating(self._refresh_wallets_job, interval=WALLET_TTL, first=30)
        jq.run_daily(
            self._daily_summary_job,
            time=datetime.strptime("08:00", "%H:%M").time().replace(tzinfo=timezone.utc),
        )
        jq.run_once(self._startup_notification_job, when=2)
        logger.info("Background started: trading loop, sentiment, wallets, daily summary")

    def _start_web_ui(self) -> str:
        """
        Spawn a uvicorn subprocess for the web dashboard.
        Returns one of: 'started', 'already_running', 'port_conflict', 'error:<msg>'.
        Non-blocking — Popen returns immediately.
        """
        import subprocess
        import sys

        global _web_ui_process
        if _web_ui_process is not None and _web_ui_process.poll() is None:
            return "already_running"
        if not _port_free(8080):
            logger.warning("Port 8080 is already occupied — skipping web server start")
            return "port_conflict"
        try:
            project_root = Path(__file__).resolve().parent.parent
            _web_ui_process = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "web.app:app", "--host", "0.0.0.0", "--port", "8080"],
                cwd=str(project_root),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
            )
            logger.info("Web UI started at http://localhost:8080 (pid=%s)", _web_ui_process.pid)
            return "started"
        except Exception as exc:
            logger.error("Failed to start web UI: %s", exc)
            return f"error:{exc}"

    def _stop_web_ui(self) -> str:
        """
        Terminate the uvicorn subprocess.
        Returns one of: 'stopped', 'not_running'.
        """
        global _web_ui_process
        if _web_ui_process is None or _web_ui_process.poll() is not None:
            _web_ui_process = None
            return "not_running"
        try:
            _web_ui_process.terminate()
            _web_ui_process.wait(timeout=5)
        except Exception:
            try:
                _web_ui_process.kill()
            except Exception:
                pass
        _web_ui_process = None
        logger.info("Web UI stopped")
        return "stopped"

    async def _startup_notification_job(self, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Send the startup summary once after background has started."""
        try:
            s = await asyncio.to_thread(self.engine.get_status)
            has_markets = bool(self.engine._active_token_ids)
            market_status = (
                f"{len(self.engine._active_token_ids)} Polymarket BTC markets loaded"
                if has_markets
                else "SIMULATION MODE (markets config empty — run scripts/find_btc_markets.py)"
            )
            try:
                from config.settings import PROJECT_ROOT
                base = "Calibration model" if (PROJECT_ROOT / "models/saved/calibration_model.joblib").exists() else "Momentum heuristic"
                llm_ok = self.engine._llm and self.engine._llm.is_available
                model_status = f"{base} + Claude LLM (learns & refines)" if llm_ok else f"{base} (set ANTHROPIC_API_KEY for Claude)"
            except Exception:
                model_status = "Model loading…"
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
                f"⚙️ Loop: every 45 s · Trades close in ~5 min\n\n"
                f"Tip: Run only *one* bot instance to avoid balance mix-ups."
            )
            await self._notify(text, reply_markup=MAIN_KEYBOARD)
        except Exception as exc:
            logger.error("Startup notification failed: %s", exc)

    # ── Commands — all heavy work offloaded to thread pool ────────────────────

    async def _start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Telegram /start convention — trading is already running. Show greeting + status."""
        msg = update.message or update.effective_message
        s = await asyncio.to_thread(self.engine.get_status)
        text = (
            f"👋 *PolyQuant is running.*\n\n"
            f"💰 Balance: ${s['balance']:.2f}  ·  Return: {s['return_pct']:+.1f}%\n"
            f"📋 {s['n_total']} trades  ·  {s['n_wins']}W / {s['n_losses']}L\n\n"
            f"🌐 Dashboard: http://localhost:8080\n\n"
            f"Use the buttons below to monitor and control the bot."
        )
        await msg.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=MAIN_KEYBOARD)

    async def _help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.message or update.effective_message
        text = (
            "*PolyQuant Paper Trading Bot*\n\n"
            "Trading starts automatically on launch. Web dashboard: http://localhost:8080\n\n"
            "Use the buttons below or type commands:\n\n"
            "📊 Status · Positions · Trades · Performance\n"
            "🌡 Sentiment · Learn · Params\n"
            "▶️ Resume · ⏸ Pause · 🔄 Reset · 🌐 Web UI\n"
            "🔧 Find Markets — discover BTC markets for trading\n\n"
            "/wallets · /live_check — advanced commands"
        )
        await msg.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=MAIN_KEYBOARD)

    async def _handle_keyboard(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Route ReplyKeyboard button presses to the correct handler."""
        text = (update.message.text or "").strip()
        mapping = {
            "📊 Status": self._status,
            "📋 Positions": self._positions,
            "📈 Trades": self._trades,
            "📉 Performance": self._performance,
            "🌡 Sentiment": self._sentiment,
            "🧠 Learn": self._learn,
            "⚙ Params": self._params,
            "▶️ Resume": self._start_bot,
            "⏸ Pause": self._stop_bot,
            "🔄 Reset": self._reset_start,
            "🌐 Web UI": self._web_ui_menu,
            "🔧 Find Markets": self._find_markets,
        }
        handler = mapping.get(text)
        if handler:
            await handler(update, ctx)
        else:
            # Unknown text — re-send keyboard so buttons are always visible
            logger.debug("Unrecognized message text: %r (len=%d, bytes=%s)", text, len(text), text.encode())
            await update.message.reply_text(
                "Use the buttons below.", reply_markup=MAIN_KEYBOARD
            )

    async def _status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        s = await asyncio.to_thread(self.engine.get_status)
        global _trading_active

        status_str = "RUNNING" if _trading_active else "PAUSED"
        mode_str = "PAPER" if settings.paper_trading else "LIVE"
        daily_pnl = s.get("daily_pnl", 0.0)
        trades_today = s.get("trades_today", 0)
        garch_vol = s.get("garch_vol")
        garch_regime = s.get("garch_regime", "—")
        garch_line = f"{garch_vol:.3f} ({garch_regime})" if garch_vol is not None else "—"
        kill_str = "triggered" if s["kill_switch"] else "armed"

        text = (
            f"*Status:* {status_str}\n"
            f"*Mode:* {mode_str}\n"
            f"*Daily P&L:* {daily_pnl:+.2f} USDC\n"
            f"*Trades today:* {trades_today}\n"
            f"*GARCH vol:* {garch_line}\n"
            f"*Open positions:* {s['n_open']}\n"
            f"*Kill switch:* {kill_str}\n\n"
            f"💰 Balance: ${s['balance']:.2f}  ·  Return: {s['return_pct']:+.1f}%\n"
            f"📋 Total: {s['n_total']} trades  ·  {s['n_wins']}W / {s['n_losses']}L  ·  {s['win_rate']:.0%} win rate"
        )
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

    async def _positions(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        positions = await asyncio.to_thread(self.engine.get_open_positions_for_display)
        if not positions:
            await update.message.reply_text("No open positions right now.")
            return

        now = datetime.now(timezone.utc)
        lines = [f"📋 *{len(positions)} open position(s)*\n"]
        for p in positions:
            age_s     = age_seconds(p["opened_at"], now)
            remaining = max(0, 300 - int(age_s))
            rem_str   = f"{remaining // 60}m{remaining % 60}s"
            lines.append(
                f"#{p['id']} {_dir_display(p['direction'])} — ${p['size_usdc']:.2f}\n"
                f"  Confidence: {p['edge']:.2f}  ·  BTC: ${p['btc_price_entry']:,.0f}\n"
                f"  Closes in ~{rem_str}\n"
            )
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

    async def _trades(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        n = 10
        if ctx.args:
            try:
                n = max(1, min(50, int(ctx.args[0])))
            except ValueError:
                pass

        def _fetch():
            trades = self.engine.get_recent_trades(n)
            status = self.engine.get_status()
            return trades, status

        trades, s = await asyncio.to_thread(_fetch)
        if not trades:
            await update.message.reply_text("No closed trades yet.")
            return

        total_str = f"+${s['total_pnl']:.2f}" if s["total_pnl"] >= 0 else f"-${abs(s['total_pnl']):.2f}"
        lines = [f"📊 *Last {len(trades)} trades* · *Total profit:* {total_str}\n"]
        cum = 0.0
        for t in trades:
            pnl = t["pnl"] or 0.0
            cum += pnl
            icon = "✅" if t["status"] == "won" else "❌"
            cum_str = f"+${cum:.2f}" if cum >= 0 else f"-${abs(cum):.2f}"
            btc_e = t.get("btc_price_entry") or 0
            btc_x = t.get("btc_price_exit") or 0
            btc_move = btc_x - btc_e
            move_str = f"+{btc_move:,.0f}" if btc_move >= 0 else f"{btc_move:,.0f}"
            lines.append(
                f"{icon} #{t['id']} {_dir_display(t['direction'])} {pnl:+.2f} (running: {cum_str})\n"
                f"  BTC: ${btc_e:,.0f} → ${btc_x:,.0f} ({move_str})\n"
            )
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

    async def _performance(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        # All computation in thread pool
        def _compute():
            s      = self.engine.get_status()
            closed = db.get_all_closed_trades()
            if not closed:
                return None, s
            pnls   = [t["pnl"] for t in closed if t["pnl"] is not None]
            brier  = self.learner._brier_score(closed)
            sharpe = self.learner._sharpe(pnls)
            rwr    = self.learner._rolling_win_rate(closed, 20)
            avg_e  = sum(t["edge"] for t in closed) / len(closed)
            avg_p  = sum(pnls) / len(pnls) if pnls else 0.0
            peak, running, max_dd = s["starting_balance"], s["starting_balance"], 0.0
            for pnl in pnls:
                running += pnl
                if running > peak:
                    peak = running
                dd = (peak - running) / peak if peak > 0 else 0.0
                max_dd = max(max_dd, dd)
            return {
                "closed": len(closed), "brier": brier, "sharpe": sharpe,
                "rwr": rwr, "avg_edge": avg_e, "avg_pnl": avg_p,
                "best": max(pnls), "worst": min(pnls), "max_dd": max_dd,
            }, s

        result, s = await asyncio.to_thread(_compute)
        if result is None:
            await update.message.reply_text("No closed trades yet. Trades close after ~5 minutes.")
            return

        text = (
            f"📈 *Performance Report*\n\n"
            f"*Trades:* {result['closed']} total  ·  {s['n_wins']} wins / {s['n_losses']} losses\n"
            f"Win rate: {s['win_rate']:.0%}  ·  Recent (20): {result['rwr']:.0%}\n\n"
            f"*Total profit:* ${s['total_pnl']:+.2f}\n"
            f"Avg per trade: ${result['avg_pnl']:+.2f}\n"
            f"Best: ${result['best']:+.2f}  ·  Worst: ${result['worst']:.2f}\n\n"
            f"Risk-adjusted score: {result['sharpe']:.2f}\n"
            f"Max drawdown: {result['max_dd']:.0%}\n\n"
            f"*Balance:* ${s['balance']:.2f} ({s['return_pct']:+.1f}% return)"
        )
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

    async def _learn(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        report = await asyncio.to_thread(self.learner.build_report)
        for chunk in split_message(report):
            await update.message.reply_text(chunk, parse_mode=ParseMode.MARKDOWN)

    async def _params(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        p = await asyncio.to_thread(self.engine.get_adaptive_params)
        base_edge  = settings.min_edge_threshold
        base_kelly = settings.kelly_fraction
        ed = p["min_edge"] - base_edge
        kd = p["kelly_fraction"] - base_kelly
        text = (
            f"⚙️ *Trading Parameters*\n\n"
            f"Min confidence: `{p['min_edge']:.2f}` "
            f"({'↑' if ed > 0 else '↓'} from base)\n"
            f"Bet size: `{p['kelly_fraction']:.2f}` "
            f"({'↑' if kd > 0 else '↓'} from base)\n"
            f"Max spread: `{p['max_spread']:.2f}`\n"
            f"Max per trade: `${settings.max_position_usdc:.2f}`\n"
            f"Daily loss limit: `{settings.max_daily_drawdown_pct:.0%}`\n\n"
            f"_Updates automatically every 5 closed trades._"
        )
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

    async def _sentiment(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Reads from cache — responds instantly. Cache refreshes every 5 min."""
        snap = self._sentiment_cache
        if snap is None:
            await update.message.reply_text(
                "⏳ Fetching sentiment data for the first time (~5s)..."
            )
            snap = await asyncio.to_thread(self.engine.get_sentiment_snapshot)
            self._sentiment_cache = snap

        if not snap or not snap.get("available"):
            await update.message.reply_text(
                "Sentiment data unavailable. Check your internet connection."
            )
            return

        fg        = snap.get("fear_greed_value", 50)
        fg_label  = snap.get("fear_greed_label", "Unknown")
        fg_icon   = _fg_emoji(fg)
        fr        = snap.get("funding_rate", 0.0)
        fr_bias   = "Market leans up" if fr < 0 else "Market leans down"
        oi        = snap.get("oi_change_pct", 0.0)
        composite = snap.get("composite_score", 0.5)
        headlines = snap.get("headlines", [])
        hl_text   = "\n".join(f"  • {h}" for h in headlines) if headlines else "  No recent headlines"

        text = (
            f"🌡️ *Market Mood* _(updates every 5 min)_\n\n"
            f"{fg_icon} *Fear & Greed:* {fg}/100 — {fg_label}\n"
            f"📉 *Funding:* {fr*100:+.2f}% — {fr_bias}\n"
            f"📊 *Position change (1h):* {oi:+.1f}%\n"
            f"🎯 *Overall:* {_composite_label(composite)}\n\n"
            f"📰 *BTC News:*\n{hl_text}"
        )
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

    async def _wallets(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Reads from cache — responds instantly. Cache refreshes every 10 min."""
        report = self._wallet_report_cache
        for chunk in split_message(report):
            await update.message.reply_text(chunk, parse_mode=ParseMode.MARKDOWN)

    async def _live_check(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text("Checking setup...")
        def _check():
            try:
                from wallets.agentkit_base import live_trading_readiness_check, get_all_balances
                return live_trading_readiness_check(), get_all_balances()
            except Exception as exc:
                return None, {"error": str(exc)}

        checks, balances = await asyncio.to_thread(_check)
        if checks is None:
            await update.message.reply_text(f"Check failed: {balances.get('error')}")
            return

        def yn(v): return "✅" if v else "❌"
        ready   = checks.get("ready_for_live", False)
        overall = "✅ *Ready for real money*" if ready else "⚠️ *Not ready yet*"
        text = (
            f"🔍 *Live Trading Setup*\n\n"
            f"{overall}\n\n"
            f"{yn(checks.get('py_clob_client_installed'))} Polymarket connection\n"
            f"{yn(checks.get('clob_api_key_set'))} API keys set\n"
            f"{yn(checks.get('wallet_address_set'))} Wallet address set\n"
            f"{yn(checks.get('markets_configured'))} Markets configured ({checks.get('n_markets', 0)})\n"
            f"{yn(checks.get('polygon_usdc_sufficient'))} Polygon USDC ≥ $10\n\n"
            f"*Balances:*\n"
            f"  Base: ${balances.get('base_usdc', 0):.2f}\n"
            f"  Polygon: ${balances.get('polygon_usdc', 0):.2f}\n\n"
            f"_Set PAPER\\_TRADING=false in .env to switch to real money_"
        )
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

    async def _find_markets(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Run BTC market discovery script and report result."""
        msg = update.message or update.effective_message
        await msg.reply_text("🔧 Discovering BTC markets from Polymarket… (may take 15–30s)")
        project_root = Path(__file__).resolve().parent.parent

        def _run():
            import subprocess
            import sys
            result = subprocess.run(
                [sys.executable, str(project_root / "scripts" / "find_btc_markets.py")],
                cwd=str(project_root),
                capture_output=True,
                text=True,
                timeout=90,
            )
            out = (result.stdout or "").strip() + "\n" + (result.stderr or "").strip()
            # Read saved config for count
            cfg_path = project_root / "config" / "btc_markets.json"
            n = 0
            if cfg_path.exists():
                try:
                    import json
                    with open(cfg_path) as f:
                        data = json.load(f)
                    n = len(data) if isinstance(data, list) else 0
                except Exception:
                    pass
            return result.returncode, n, out[:800]

        try:
            code, n_markets, log = await asyncio.to_thread(_run)
            if code == 0:
                await msg.reply_text(
                    f"✅ *Find Markets complete*\n\n"
                    f"Saved *{n_markets}* market(s) to `config/btc_markets.json`.\n"
                    f"Restart the bot or wait for next cycle to use them.",
                    parse_mode=ParseMode.MARKDOWN,
                )
            else:
                await msg.reply_text(
                    f"⚠️ Find Markets finished with errors.\n\n"
                    f"Markets in config: *{n_markets}*\n\n"
                    f"Log:\n`{log[-400:]}`",
                    parse_mode=ParseMode.MARKDOWN,
                )
        except asyncio.TimeoutError:
            await msg.reply_text("⏱ Script timed out (90s). Try again or run manually: `python scripts/find_btc_markets.py`")
        except Exception as exc:
            await msg.reply_text(f"❌ Find Markets failed: {exc}")

    async def _web_ui_menu(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Web UI controller: Start / Stop / Open. Also callable via /webui command."""
        global _web_ui_process
        is_running = _web_ui_process is not None and _web_ui_process.poll() is None
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("▶️ Start Web UI", callback_data="webui_start")],
            [InlineKeyboardButton("⏹ Stop Web UI", callback_data="webui_stop")],
            [InlineKeyboardButton("🌐 Open http://localhost:8080", callback_data="webui_open")],
        ])
        status = "🟢 Running on http://localhost:8080" if is_running else "⚪ Stopped"
        msg = update.message or update.effective_message
        await msg.reply_text(
            f"🌐 *Web UI Controller*\n\nStatus: {status}\n\n"
            "Start: launches the dashboard\nStop: shuts it down\nOpen: link to dashboard",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=keyboard,
        )

    async def _web_ui_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle Web UI Start / Stop / Open callbacks."""
        query = update.callback_query
        await query.answer()

        if query.data == "webui_start":
            result = self._start_web_ui()
            if result == "already_running":
                await query.edit_message_text("🌐 Web UI is already running at http://localhost:8080")
            elif result == "started":
                await query.edit_message_text(
                    "🌐 Web UI started at http://localhost:8080\n\nOpen in your browser."
                )
            elif result == "port_conflict":
                await query.edit_message_text(
                    "⚠️ Port 8080 is already occupied by another process.\n"
                    "Stop the other process and try again."
                )
            else:
                await query.edit_message_text(f"❌ Failed to start Web UI: {result}")

        elif query.data == "webui_stop":
            result = self._stop_web_ui()
            if result == "not_running":
                await query.edit_message_text("⚪ Web UI was not running.")
            else:
                await query.edit_message_text("⏹ Web UI stopped.")

        else:  # webui_open
            await query.edit_message_text(
                "🌐 Open the dashboard in your browser:\n\nhttp://localhost:8080"
            )

    async def _reset_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.message or update.effective_message
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Confirm Reset", callback_data="reset_confirm")],
            [InlineKeyboardButton("❌ Cancel", callback_data="reset_cancel")],
        ])
        await msg.reply_text(
            "⚠️ *Reset Paper Balance*\n\n"
            "Resets balance to $1,000. Trade history is kept.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=keyboard,
        )

    async def _reset_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        if query.data == "reset_confirm":
            await asyncio.to_thread(self.engine.reset_balance, 1_000.0)
            await query.edit_message_text("✅ Paper balance reset to $1,000.00 USDC.")
        else:
            await query.edit_message_text("❌ Reset cancelled.")

    async def _start_bot(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        global _trading_active
        _trading_active = True
        try:
            override_path = Path(__file__).resolve().parent.parent / "config" / "mode_override.json"
            override_path.parent.mkdir(parents=True, exist_ok=True)
            data = {}
            if override_path.exists():
                import json
                with open(override_path) as f:
                    data = json.load(f)
            data["trading_paused"] = False
            with open(override_path, "w") as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass
        web_status = self._start_web_ui()
        web_note = "" if web_status in ("started", "already_running") else f"\n⚠️ Web server: {web_status}"
        await update.message.reply_text(
            f"▶️ Trading resumed. Web dashboard at http://localhost:8080{web_note}",
            parse_mode=ParseMode.MARKDOWN,
        )

    async def _stop_bot(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        global _trading_active
        _trading_active = False
        try:
            override_path = Path(__file__).resolve().parent.parent / "config" / "mode_override.json"
            override_path.parent.mkdir(parents=True, exist_ok=True)
            data = {}
            if override_path.exists():
                import json
                with open(override_path) as f:
                    data = json.load(f)
            data["trading_paused"] = True
            with open(override_path, "w") as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass
        self._stop_web_ui()
        await update.message.reply_text(
            "⏸ Trading paused. Web server stopped.\nOpen positions will still close. Tap ▶️ Resume to continue.",
            parse_mode=ParseMode.MARKDOWN,
        )

    # ── Background Jobs ────────────────────────────────────────────────────────

    async def _trading_loop_job(self, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """
        Trading cycle — runs blocking engine.run_cycle() in a thread pool.
        The event loop stays free, so commands respond instantly even while
        the trading cycle is running.
        Pause can come from Telegram (⏸ Pause) or Web UI (writes mode_override.json).
        """
        global _trading_active
        if not _trading_active:
            return
        # Web UI pause (mode_override.json)
        try:
            override_path = Path(__file__).resolve().parent.parent / "config" / "mode_override.json"
            if override_path.exists():
                import json
                with open(override_path) as f:
                    ov = json.load(f)
                if ov.get("trading_paused"):
                    return
        except Exception:
            pass
        try:
            events = await asyncio.to_thread(self.engine.run_cycle)
        except Exception as exc:
            logger.error("Trading cycle error: %s\n%s", exc, traceback.format_exc())
            await self._notify(f"⚠️ Trading cycle error: `{exc}`")
            return

        for event in events:
            await self._handle_event(event)

        # Check for learning update (pure computation, thread pool)
        try:
            insight = await asyncio.to_thread(self.learner.maybe_learn)
            if insight and insight.get("n", 0) > 0:
                await self._notify(_format_learning_update(insight))
        except Exception as exc:
            logger.error("Learner error: %s", exc)

    async def _refresh_sentiment_job(self, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Refresh sentiment cache in background — never blocks commands."""
        try:
            snap = await asyncio.to_thread(self.engine.get_sentiment_snapshot)
            self._sentiment_cache = snap
            logger.debug("Sentiment cache refreshed")
        except Exception as exc:
            logger.warning("Sentiment refresh failed: %s", exc)

    async def _refresh_wallets_job(self, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Refresh wallet report cache in background — never blocks commands."""
        try:
            def _build_wallet_report():
                from data.wallet_tracker import WalletTracker
                tracker = WalletTracker()
                return tracker.build_report()

            report = await asyncio.to_thread(_build_wallet_report)
            self._wallet_report_cache = report
            logger.debug("Wallet cache refreshed")
        except Exception as exc:
            logger.warning("Wallet refresh failed (non-critical): %s", exc)
            # Keep previous cache, don't overwrite with error

    async def _daily_summary_job(self, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            def _build():
                s      = self.engine.get_status()
                closed = db.get_all_closed_trades()
                rwr    = self.learner._rolling_win_rate(closed, 20) if closed else 0.0
                return s, rwr

            s, rwr = await asyncio.to_thread(_build)
            text = (
                f"🌅 *Daily Summary*\n\n"
                f"Balance: ${s['balance']:.2f} ({s['return_pct']:+.1f}% return)\n"
                f"Total profit: ${s['total_pnl']:+.2f}\n"
                f"Trades: {s['n_total']} · {s['n_wins']} wins / {s['n_losses']} losses\n"
                f"Win rate: {s['win_rate']:.0%} · Recent: {rwr:.0%}\n"
                f"Safety stop: {'🔴 Active' if s['kill_switch'] else '🟢 OK'}"
            )
            await self._notify(text)
        except Exception as exc:
            logger.error("Daily summary error: %s", exc)

    # ── Event Dispatching ──────────────────────────────────────────────────────

    async def _handle_event(self, event: dict[str, Any]) -> None:
        etype = event.get("type")
        try:
            if etype == "trade_opened":
                await self._notify(_format_trade_opened(event))
            elif etype == "trade_resolved":
                await self._notify(_format_trade_resolved(event))
            elif etype == "kill_switch":
                global _trading_active
                _trading_active = False
                await self._notify(
                    f"🚨 *Safety Stop Activated*\n\n"
                    f"Reason: {event['reason']}\n"
                    f"Bankroll: ${event['balance']:.2f}\n\n"
                    f"Trading paused. Send /reset to reset balance, then tap ▶️ Resume to restart."
                )
        except Exception as exc:
            logger.error("Event notification error: %s", exc)

    async def _notify(self, text: str, reply_markup=None) -> None:
        try:
            chunks = split_message(text)
            for i, chunk in enumerate(chunks):
                kwargs = dict(chat_id=self._chat_id, text=chunk, parse_mode=ParseMode.MARKDOWN)
                if reply_markup and i == len(chunks) - 1:
                    kwargs["reply_markup"] = reply_markup
                await self.app.bot.send_message(**kwargs)
        except TelegramError as exc:
            if "parse" in str(exc).lower():
                try:
                    await self.app.bot.send_message(
                        chat_id=self._chat_id, text=text, reply_markup=reply_markup
                    )
                except Exception:
                    pass
            else:
                logger.error("Telegram send error: %s", exc)

    async def _require_authorized(self, update: Update) -> bool:
        """Return True if update is from the configured chat_id; otherwise reply 'Unauthorized' and return False."""
        if update.effective_chat is None:
            return False
        if str(update.effective_chat.id) != str(self._chat_id):
            try:
                await update.effective_message.reply_text("Unauthorized.")
            except Exception:
                pass
            return False
        return True

    def _wrap_cmd(self, handler):
        """Wrap a command/callback handler to enforce chat_id authorization."""
        async def wrapped(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
            if not await self._require_authorized(update):
                return
            await handler(update, ctx)
        return wrapped

    # ── Setup ──────────────────────────────────────────────────────────────────

    def _register_handlers(self) -> None:
        add = self.app.add_handler
        # Inline keyboard callbacks (confirm/cancel pop-ups)
        add(CallbackQueryHandler(self._wrap_cmd(self._reset_callback), pattern="^reset_(confirm|cancel)$"))
        add(CallbackQueryHandler(self._wrap_cmd(self._web_ui_callback), pattern="^webui_(start|stop|open)$"))
        # ALL non-command text → button router (exact dict match, no regex fragility)
        add(MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            self._wrap_cmd(self._handle_keyboard),
        ))
        # /start — Telegram requires this for the bot to be discoverable
        # /wallets and /live_check have no keyboard button; keep as commands
        # /trades keeps its optional [n] argument via command
        for cmd, handler in [
            ("start", self._start),
            ("wallets", self._wallets),
            ("live_check", self._live_check),
            ("trades", self._trades),
        ]:
            add(CommandHandler(cmd, self._wrap_cmd(handler)))


# ── Signal Formatters ─────────────────────────────────────────────────────────

def _format_trade_opened(e: dict) -> str:
    direction = e["direction"]
    dir_icon  = "📈 UP" if direction == "YES" else "📉 DOWN"
    btc       = e.get("btc_price", 0)
    edge      = e.get("edge", 0)
    fg        = e.get("fear_greed")
    fr        = e.get("funding_rate")
    whale     = e.get("whale_alignment", "NEUTRAL")
    sent      = e.get("sentiment_score")
    is_sim    = e.get("simulated", False)

    conf = "⚡ Strong" if edge > 0.08 else ("✅ Good" if edge > 0.05 else "🔍 Moderate")
    header = "📤 *New Trade*" if not is_sim else "🔬 *Simulated*"

    lines = [
        f"{header} #{e['trade_id']}",
        f"",
        f"{dir_icon}  Confidence: {conf}",
        f"",
        f"💵 Bet: ${e['size_usdc']:.2f}",
        f"₿ BTC: ${btc:,.0f}",
    ]
    if fg is not None:
        lines.append(f"🌡 Fear & Greed: {fg}/100 {_fg_emoji(fg)}")
    if fr is not None:
        bias = "market up" if fr < 0 else "market down"
        lines.append(f"Funding: {fr*100:+.2f}% ({bias})")
    if whale and whale != "NEUTRAL":
        lines.append(f"🐋 Whales: {whale}")
    if sent is not None:
        lines.append(f"🌡 Mood: {_composite_label(sent)}")
    lines += ["", f"💼 Bankroll: ${e['balance']:.2f} (after this bet)", "_Closes in ~5 minutes_"]
    return "\n".join(lines)


def _format_trade_resolved(e: dict) -> str:
    icon      = "✅ Won" if e["won"] else "❌ Lost"
    pnl       = e["pnl"]
    pnl_str   = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
    btc_move  = e["btc_exit"] - e["btc_entry"]
    move_str  = f"+${btc_move:,.0f}" if btc_move >= 0 else f"-${abs(btc_move):,.0f}"
    dir_str   = _dir_display(e["direction"])
    total_pnl = e.get("total_pnl", e["balance"] - 1000.0)
    total_str = f"+${total_pnl:.2f}" if total_pnl >= 0 else f"-${abs(total_pnl):.2f}"
    return (
        f"{icon} *Trade #{e['trade_id']} Closed*\n\n"
        f"{dir_str}  ·  {pnl_str} this trade\n"
        f"{('Profit ' + pnl_str + ' added to bankroll ✓\n') if pnl > 0 else ('Loss ' + pnl_str + ' deducted\n') if pnl < 0 else ''}"
        f"BTC: ${e['btc_entry']:,.2f} → ${e['btc_exit']:,.2f} ({move_str})\n"
        f"💼 Bankroll: ${e['balance']:.2f}\n"
        f"📊 Total profit so far: {total_str}"
    )


def _format_learning_update(insight: dict) -> str:
    notes     = insight.get("notes", [])
    notes_str = "\n".join(f"  • {n}" for n in notes) if notes else "  No changes"
    return (
        f"🧠 *Learning Update* ({insight['n']} trades)\n\n"
        f"Win rate: {insight.get('win_rate', 0):.0%}  ·  Recent: {insight.get('recent_win_rate', 0):.0%}\n"
        f"Avg profit: ${insight.get('avg_pnl', 0):+.2f}\n\n"
        f"Changes:\n{notes_str}\n\n"
        f"Confidence: {insight.get('new_edge', 0):.2f}  ·  Bet size: {insight.get('new_kelly', 0):.2f}"
    )


# ── Helpers ────────────────────────────────────────────────────────────────────

def _dir_display(direction: str) -> str:
    """Map YES/NO to UP/DOWN for user display (all outcomes logged for model learning)."""
    return "UP" if (direction or "").upper() == "YES" else "DOWN"


def _fg_emoji(v: int) -> str:
    return "😱" if v <= 25 else "😨" if v <= 45 else "😐" if v <= 55 else "😏" if v <= 75 else "🤑"


def _composite_label(s: float) -> str:
    if s < 0.25:  return "Very Bearish 🐻"
    if s < 0.40:  return "Bearish 📉"
    if s < 0.60:  return "Neutral ⚖️"
    if s < 0.75:  return "Bullish 📈"
    return "Very Bullish 🚀"


