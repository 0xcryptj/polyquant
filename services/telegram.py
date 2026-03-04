"""
TelegramService — thin Telegram interface layer. Zero business logic.

All commands delegate to ctx.engine, ctx.learner, or persistence.db.
This service does NOT own the trading loop, data refresh, or learner.
Those are owned by TradingService and DataService respectively.

Commands:
  /status /health /pause /resume /kill
  /positions /trades /performance /learn /params
  /sentiment /wallets /reset /live_check /find_markets /config
"""
from __future__ import annotations

import asyncio
import os
import subprocess
from datetime import datetime, timezone
from typing import Any

import structlog
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
from paper_trading import persistence as db
from paper_trading.utils import age_seconds, split_message
from runtime.context import BOT_VERSION, RuntimeContext
from services.base import BaseService, HealthStatus

log = structlog.get_logger("telegram")

# Reply keyboard — always visible at the bottom of the chat
KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton("🚀 Start"),       KeyboardButton("▶️ Resume"),      KeyboardButton("⏸ Pause")],
        [KeyboardButton("📊 Status"),      KeyboardButton("📋 Positions"),   KeyboardButton("📈 Trades")],
        [KeyboardButton("📉 Performance"), KeyboardButton("🌡 Sentiment"),   KeyboardButton("🧠 Learn")],
        [KeyboardButton("💊 Health"),      KeyboardButton("🔧 Markets"),     KeyboardButton("🛑 Kill")],
    ],
    resize_keyboard=True,
)


def _inline_home_keyboard(admin: bool) -> InlineKeyboardMarkup:
    """Inline keyboard for control panel. Admin sees write buttons."""
    row1 = [
        InlineKeyboardButton("📊 Status", callback_data="cb:status"),
        InlineKeyboardButton("📋 Positions", callback_data="cb:positions"),
        InlineKeyboardButton("📈 PnL", callback_data="cb:pnl"),
    ]
    row2 = [
        InlineKeyboardButton("💊 Providers", callback_data="cb:providers"),
        InlineKeyboardButton("🌐 WebUI", callback_data="cb:webui"),
        InlineKeyboardButton("👛 Wallet UI", callback_data="cb:wallet_ui"),
    ]
    if admin:
        row3 = [
            InlineKeyboardButton("▶️ Start Paper", callback_data="cb:start_paper"),
            InlineKeyboardButton("⏸ Pause", callback_data="cb:pause"),
        ]
        row4 = [InlineKeyboardButton("🛑 Kill Switch", callback_data="cb:kill_switch")]
        return InlineKeyboardMarkup([row1, row2, row3, row4])
    return InlineKeyboardMarkup([row1, row2])


class TelegramService(BaseService):
    name = "telegram"

    def __init__(self, ctx: RuntimeContext) -> None:
        super().__init__(ctx)
        token         = settings.telegram_bot_token.get_secret_value()
        self.app      = Application.builder().token(token).updater(None).build()
        self._chat_id = settings.telegram_chat_id
        self._task:        asyncio.Task | None = None
        self._poll_errors: int                 = 0

        # Wire alert hook so SupervisorService and TradingService can push alerts
        ctx._telegram_notify = self.notify

        self._register_handlers()

    # ── BaseService ───────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._task = asyncio.create_task(
            self._polling_loop(), name="telegram-poll"
        )
        self._mark_started()
        log.info("started")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        try:
            await self.app.stop()
            await self.app.shutdown()
        except Exception:
            pass
        log.info("stopped", poll_errors=self._poll_errors)

    async def health(self) -> HealthStatus:
        if self._task and self._task.done():
            return self._health_fail("poll loop dead",
                                     errors=self._poll_errors)
        return self._health_ok(poll_errors=self._poll_errors)

    def status(self) -> dict[str, Any]:
        base = super().status()
        base["poll_errors"] = self._poll_errors
        return base

    # ── Public notification API ───────────────────────────────────────────────

    async def notify(self, text: str) -> None:
        """Send a message to the configured chat. Splits long messages."""
        if not text:
            return
        try:
            for chunk in split_message(text):
                await self.app.bot.send_message(
                    chat_id=self._chat_id,
                    text=chunk,
                    parse_mode=ParseMode.MARKDOWN,
                )
        except TelegramError as exc:
            if "parse" in str(exc).lower():
                try:
                    await self.app.bot.send_message(
                        chat_id=self._chat_id, text=text
                    )
                except Exception:
                    pass
            else:
                log.warning("send_error", error=str(exc))

    # ── Polling loop ──────────────────────────────────────────────────────────

    async def _polling_loop(self) -> None:
        import httpx

        def _clear_webhook() -> str:
            with httpx.Client(timeout=10.0) as c:
                return c.get(
                    f"https://api.telegram.org/bot"
                    f"{settings.telegram_bot_token.get_secret_value()}"
                    "/deleteWebhook?drop_pending_updates=true"
                ).text[:80]

        try:
            txt = await asyncio.to_thread(_clear_webhook)
            log.debug("webhook_cleared", response=txt)
        except Exception as exc:
            log.warning("webhook_clear_failed", error=str(exc))

        offset, conflict_count = 0, 0
        await self.app.initialize()
        await self.app.start()
        asyncio.create_task(self._send_startup())

        try:
            while not self.ctx.shutdown_event.is_set():
                try:
                    updates = await self.app.bot.get_updates(
                        offset=offset,
                        timeout=0,
                        allowed_updates=["message", "callback_query"],
                    )
                    conflict_count = 0
                    for upd in updates:
                        await self.app.process_update(upd)
                        offset = upd.update_id + 1
                    await asyncio.sleep(1)

                except TelegramError as exc:
                    if "Conflict" in str(exc):
                        conflict_count += 1
                        wait = min(5 * conflict_count, 45)
                        log.warning("conflict",
                                    count=conflict_count, wait_s=wait)
                        await asyncio.sleep(wait)
                    else:
                        self._poll_errors += 1
                        log.error("poll_error", error=str(exc))
                        await asyncio.sleep(5)

                except asyncio.CancelledError:
                    break
                except Exception as exc:
                    self._poll_errors += 1
                    log.error("poll_error", error=str(exc))
                    await asyncio.sleep(5)

        finally:
            try:
                await self.app.stop()
                await self.app.shutdown()
            except Exception:
                pass

    async def _send_startup(self) -> None:
        await asyncio.sleep(3)
        try:
            s = await asyncio.to_thread(self.ctx.engine.get_status)
            # Send startup message with reply keyboard — establishes the persistent bottom keyboard
            await self.app.bot.send_message(
                chat_id=self._chat_id,
                text=(
                    f"🚀 *PolyQuant v{BOT_VERSION} online*\n\n"
                    f"💰 ${s['balance']:.2f}  ·  {s['return_pct']:+.1f}%\n"
                    f"📋 {s['n_total']} trades  ·  {s['n_wins']}W / {s['n_losses']}L\n\n"
                    f"Tap *🚀 Start* to begin trading or use any button below."
                ),
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=KEYBOARD,
            )
            # Send inline control panel as a second message
            await self.app.bot.send_message(
                chat_id=self._chat_id,
                text=f"🏠 *Control Panel* — v{BOT_VERSION}",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=_inline_home_keyboard(admin=True),
            )
        except Exception as exc:
            log.warning("startup_notify_failed", error=str(exc))

    # ── Auth guard ────────────────────────────────────────────────────────────

    def _allowed_chat(self, update: Update) -> bool:
        """True if message/query is from the configured chat."""
        chat = update.effective_chat
        if chat is None:
            return False
        return str(chat.id) == str(self._chat_id)

    def _is_admin(self, update: Update) -> bool:
        """True if user is admin (TELEGRAM_ADMIN_ID or fallback to chat_id)."""
        user = update.effective_user
        if user is None:
            return False
        admin_id = settings.telegram_admin_id_str
        return str(user.id) == str(admin_id)

    def _wrap(self, fn):
        async def _guarded(
            update: Update, ctx: ContextTypes.DEFAULT_TYPE
        ) -> None:
            if not self._allowed_chat(update):
                try:
                    msg = update.effective_message or (update.callback_query and update.callback_query.message)
                    if msg:
                        await msg.reply_text("Unauthorized.")
                except Exception:
                    pass
                return
            await fn(update, ctx)
        return _guarded

    # ── Commands ──────────────────────────────────────────────────────────────

    # ── Inline callback router ────────────────────────────────────────────────

    async def _cb_router(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Route inline button callbacks (cb:action)."""
        query = update.callback_query
        if not query or not query.data or not query.data.startswith("cb:"):
            return
        await query.answer()
        action = query.data[3:].strip()
        admin = self._is_admin(update)
        # Write actions require admin
        if action in ("start_paper", "pause", "kill_switch", "kill_confirm", "wallet_ui") and not admin:
            await query.edit_message_text("🔒 Admin only.")
            return
        handlers = {
            "home": self._cb_home,
            "start_paper": self._cb_start_paper,
            "pause": self._cb_pause,
            "status": self._cb_status,
            "pnl": self._cb_pnl,
            "positions": self._cb_positions,
            "webui": self._cb_webui,
            "providers": self._cb_providers,
            "wallet_ui": self._cb_wallet_ui,
            "kill_switch": self._cb_kill_switch,
            "kill_confirm": self._cb_kill_confirm,
        }
        handler = handlers.get(action)
        if handler:
            await handler(update, ctx)
        else:
            await query.edit_message_text("Unknown action.")

    async def _cb_home(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        await q.edit_message_text(
            f"🏠 *PolyQuant v{BOT_VERSION}*\n\nSelect an action:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_inline_home_keyboard(self._is_admin(update)),
        )

    async def _cb_start_paper(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        ks = getattr(self.ctx.engine, "kill_switch", None)
        if ks is not None and ks.is_triggered():
            await update.callback_query.edit_message_text(
                "⚠️ Kill switch active — use /reset to clear drawdown first."
            )
            return
        self.ctx.trading_active.set()
        if self.ctx.state_store is not None:
            self.ctx.state_store.update(enabled=True)
        log.info("trading.resumed", source="telegram", via="inline")
        await update.callback_query.edit_message_text(
            "▶️ Paper trading started.",
            reply_markup=_inline_home_keyboard(self._is_admin(update)),
        )

    async def _cb_pause(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        self.ctx.trading_active.clear()
        if self.ctx.state_store is not None:
            self.ctx.state_store.update(enabled=False)
        log.info("trading.paused", source="telegram", via="inline")
        await update.callback_query.edit_message_text(
            "⏸ Trading paused. Open positions will still close.",
            reply_markup=_inline_home_keyboard(self._is_admin(update)),
        )

    async def _cb_status(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        s = await asyncio.to_thread(self.ctx.engine.get_status)
        trading = "RUNNING" if self.ctx.trading_active.is_set() else "PAUSED"
        mode = "PAPER" if settings.paper_trading else "LIVE"
        err = f"`{self.ctx.last_error[:100]}`" if self.ctx.last_error else "none"
        ks = "🔴 triggered" if s["kill_switch"] else "🟢 armed"
        text = (
            f"*Status*  ·  v{BOT_VERSION}  ·  {mode}\n"
            f"Loop: *{trading}*  ·  Uptime: {self.ctx.uptime_str}\n\n"
            f"💰 ${s['balance']:.2f}  ({s['return_pct']:+.1f}%)\n"
            f"📋 {s['n_total']} trades  ·  {s['n_wins']}W / {s['n_losses']}L"
            f"  ·  {s['win_rate']:.0%}\n"
            f"🔒 Open: {s['n_open']}  ·  Kill switch: {ks}\n\n"
            f"*Last error:* {err}"
        )
        await update.callback_query.edit_message_text(
            text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_inline_home_keyboard(self._is_admin(update)),
        )

    async def _cb_pnl(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        def _compute():
            s      = self.ctx.engine.get_status()
            closed = db.get_all_closed_trades()
            if not closed:
                return None, s
            pnls   = [t["pnl"] for t in closed if t["pnl"] is not None]
            brier  = self.ctx.learner._brier_score(closed)
            sharpe = self.ctx.learner._sharpe(pnls)
            rwr    = self.ctx.learner._rolling_win_rate(closed, 20)
            pnl_vals = pnls or [0.0]
            peak, running, max_dd = s["starting_balance"], s["starting_balance"], 0.0
            for p in pnls:
                running += p
                if running > peak:
                    peak = running
                dd = (peak - running) / peak if peak > 0 else 0.0
                max_dd = max(max_dd, dd)
            return {
                "closed": len(closed), "brier": brier, "sharpe": sharpe, "rwr": rwr,
                "avg_pnl": sum(pnls) / len(pnls) if pnls else 0.0,
                "best":    max(pnl_vals), "worst": min(pnl_vals), "max_dd": max_dd,
            }, s

        r, s = await asyncio.to_thread(_compute)
        if r is None:
            text = "No closed trades yet."
        else:
            from bot.telegram_bot import _format_pnl_card
            text = _format_pnl_card(r, s)
        await update.callback_query.edit_message_text(
            text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_inline_home_keyboard(self._is_admin(update)),
        )

    async def _cb_positions(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        positions = await asyncio.to_thread(
            self.ctx.engine.get_open_positions_for_display
        )
        if not positions:
            text = "No open positions."
        else:
            now = datetime.now(timezone.utc)
            lines = [f"📋 *{len(positions)} open*\n"]
            for p in positions:
                age = age_seconds(p["opened_at"], now)
                rem = max(0, 300 - int(age))
                d = "UP" if p["direction"] == "YES" else "DOWN"
                lines.append(
                    f"#{p['id']} {d}  ${p['size_usdc']:.2f}"
                    f"  BTC=${p['btc_price_entry']:,.0f}"
                    f"  ~{rem//60}m{rem%60}s"
                )
            text = "\n".join(lines)
        await update.callback_query.edit_message_text(
            text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_inline_home_keyboard(self._is_admin(update)),
        )

    async def _cb_webui(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        s = await asyncio.to_thread(self.ctx.engine.get_status)
        trading = "▶️ RUNNING" if self.ctx.trading_active.is_set() else "⏸ PAUSED"
        ks = "🔴 triggered" if s["kill_switch"] else "🟢 armed"
        port = settings.web_port
        text = (
            f"🌐 *Live Dashboard Snapshot*\n\n"
            f"💰 Balance: ${s['balance']:.2f}  ({s['return_pct']:+.1f}%)\n"
            f"📋 {s['n_total']} trades  ·  {s['n_wins']}W / {s['n_losses']}L  ·  {s['win_rate']:.0%}\n"
            f"🔒 Open: {s['n_open']}  ·  Kill switch: {ks}\n"
            f"Status: {trading}\n\n"
            f"Full dashboard: `http://localhost:{port}`\n"
            f"_(open on the machine running the bot)_"
        )
        await update.callback_query.edit_message_text(
            text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_inline_home_keyboard(self._is_admin(update)),
        )

    async def _cb_providers(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        lines = ["💊 *Providers*\n"]
        # Polymarket CLI
        pm_ok = "not configured"
        if getattr(settings, "polymarket_cli_cmd", None):
            try:
                from execution.providers.polymarket_cli_provider import healthcheck as pm_healthcheck
                pm_ok = "✅" if pm_healthcheck() else "❌"
            except Exception as e:
                pm_ok = f"❌ {str(e)[:40]}"
        lines.append(f"Polymarket CLI: {pm_ok}")
        # AWAL / Agentic
        awal_ok = "not configured"
        wp = self.ctx.wallet_provider
        if wp and getattr(wp, "name", "") in ("agentic", "awal"):
            try:
                awal_ok = "✅" if self.ctx.wallet_provider.health() else "❌"
            except Exception as e:
                awal_ok = f"❌ {str(e)[:40]}"
        else:
            awal_ok = "not configured (wallet=sdk or none)"
        lines.append(f"AWAL: {awal_ok}")
        text = "\n".join(lines)
        await update.callback_query.edit_message_text(
            text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_inline_home_keyboard(self._is_admin(update)),
        )

    async def _cb_wallet_ui(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        awal_bin = settings.awal_bin or "awal"
        try:
            r = subprocess.run(
                [awal_bin, "show"],
                capture_output=True,
                text=True,
                timeout=5,
                env=dict(os.environ),
            )
            if r.returncode == 0:
                text = "👛 Wallet UI launch attempted. In WSL with WSLg, a window should open."
            else:
                text = (
                    f"👛 *Wallet UI*\n\n"
                    f"Run in WSL:\n`{awal_bin} show`\n\n"
                    f"If no window opens: install WSLg, or use the CLI."
                )
        except FileNotFoundError:
            text = (
                f"👛 *Wallet UI*\n\n"
                f"`{awal_bin}` not found. Install Coinbase Agentic Wallet.\n\n"
                f"In WSL, run:\n`{awal_bin} show`\n\n"
                f"Deposit Base USDC to your wallet address."
            )
        except Exception as e:
            text = (
                f"👛 *Wallet UI*\n\n"
                f"Run in WSL: `{awal_bin} show`\n\n"
                f"Error: {str(e)[:80]}"
            )
        await update.callback_query.edit_message_text(
            text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_inline_home_keyboard(self._is_admin(update)),
        )

    async def _cb_kill_switch(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await update.callback_query.edit_message_text(
            "🛑 *Confirm emergency stop?*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("☠️ CONFIRM", callback_data="cb:kill_confirm"),
                InlineKeyboardButton("❌ Cancel", callback_data="cb:home"),
            ]]),
        )

    async def _cb_kill_confirm(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        self.ctx.trading_active.clear()
        if self.ctx.state_store is not None:
            self.ctx.state_store.update(enabled=False)
        self.ctx.record_error("Emergency stop by operator (inline)")
        log.warning("emergency_stop", source="telegram", via="inline")
        await update.callback_query.edit_message_text(
            "☠️ Trading halted. Use Start Paper to restart.",
            reply_markup=_inline_home_keyboard(self._is_admin(update)),
        )

    async def _cmd_start(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Primary entry: resume trading, restore both keyboards."""
        msg = update.message or update.effective_message
        # Resume trading unless kill switch is active
        ks = getattr(self.ctx.engine, "kill_switch", None)
        if ks is None or not ks.is_triggered():
            self.ctx.trading_active.set()
            if self.ctx.state_store is not None:
                self.ctx.state_store.update(enabled=True)
        # Re-establish the persistent reply keyboard
        await msg.reply_text(
            "🚀 Bot active — use the keyboard below or the control panel:",
            reply_markup=KEYBOARD,
        )
        # Show inline control panel
        await msg.reply_text(
            f"🏠 *PolyQuant v{BOT_VERSION}*\n\nSelect an action:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_inline_home_keyboard(self._is_admin(update)),
        )

    # ── Commands ──────────────────────────────────────────────────────────────

    async def _cmd_status(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        s       = await asyncio.to_thread(self.ctx.engine.get_status)
        trading = "RUNNING" if self.ctx.trading_active.is_set() else "PAUSED"
        mode    = "PAPER" if settings.paper_trading else "LIVE"
        err     = f"`{self.ctx.last_error[:100]}`" if self.ctx.last_error else "none"
        ks      = "🔴 triggered" if s["kill_switch"] else "🟢 armed"
        text = (
            f"*Status*  ·  v{BOT_VERSION}  ·  {mode}\n"
            f"Loop: *{trading}*  ·  Uptime: {self.ctx.uptime_str}\n\n"
            f"💰 ${s['balance']:.2f}  ({s['return_pct']:+.1f}%)\n"
            f"📋 {s['n_total']} trades  ·  {s['n_wins']}W / {s['n_losses']}L"
            f"  ·  {s['win_rate']:.0%}\n"
            f"🔒 Open: {s['n_open']}  ·  Kill switch: {ks}\n\n"
            f"*Last error:* {err}"
        )
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

    async def _cmd_health(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.message or update.effective_message

        def _check() -> dict:
            result: dict = {}
            try:
                db.get_all_closed_trades()
                result["db"] = True
            except Exception as exc:
                result["db"]       = False
                result["db_error"] = str(exc)[:60]
            try:
                s                  = self.ctx.engine.get_status()
                result["engine"]   = True
                result["open_pos"] = s.get("n_open", 0)
            except Exception as exc:
                result["engine"]       = False
                result["engine_error"] = str(exc)[:60]
            return result

        c = await asyncio.to_thread(_check)
        yn = lambda v: "✅" if v else "❌"
        lines = [
            f"💊 *Health*  ·  {self.ctx.uptime_str} uptime\n",
            f"{yn(c.get('db'))} Database",
            f"{yn(c.get('engine'))} Engine",
            f"{yn(self.ctx.trading_active.is_set())} Trading active",
            f"📊 Open positions: {c.get('open_pos', '?')}",
        ]
        if c.get("db_error"):
            lines.append(f"  DB: `{c['db_error']}`")
        if c.get("engine_error"):
            lines.append(f"  Engine: `{c['engine_error']}`")
        if self.ctx.last_error:
            lines.append(f"\n⚠️ *Last error:*\n`{self.ctx.last_error[:150]}`")
        await msg.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

    async def _cmd_pause(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        self.ctx.trading_active.clear()
        if self.ctx.state_store is not None:
            self.ctx.state_store.update(enabled=False)
        log.info("trading.paused", source="telegram")
        await update.message.reply_text(
            "⏸ Trading paused. Open positions will still close.\n"
            "Use /resume or ▶️ Resume to restart."
        )

    async def _cmd_resume(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        ks = getattr(self.ctx.engine, "kill_switch", None)
        if ks is not None and ks.is_triggered():
            await update.message.reply_text(
                "⚠️ Kill switch active — use /reset to clear drawdown first."
            )
            return
        self.ctx.trading_active.set()
        if self.ctx.state_store is not None:
            self.ctx.state_store.update(enabled=True)
        log.info("trading.resumed", source="telegram")
        await update.message.reply_text("▶️ Trading resumed.")

    async def _cmd_kill(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.message or update.effective_message
        kb  = InlineKeyboardMarkup([[
            InlineKeyboardButton("☠️ CONFIRM KILL", callback_data="kill_confirm"),
            InlineKeyboardButton("❌ Cancel",        callback_data="kill_cancel"),
        ]])
        await msg.reply_text(
            "🛑 *Emergency stop — confirm?*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb,
        )

    async def _cb_kill(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        if query.data == "kill_confirm":
            self.ctx.trading_active.clear()
            if self.ctx.state_store is not None:
                self.ctx.state_store.update(enabled=False)
            self.ctx.record_error("Emergency stop by operator")
            log.warning("emergency_stop", source="telegram")
            await query.edit_message_text(
                "☠️ Trading halted. Use /resume to restart."
            )
        else:
            await query.edit_message_text("❌ Cancelled.")

    async def _cmd_positions(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        positions = await asyncio.to_thread(
            self.ctx.engine.get_open_positions_for_display
        )
        if not positions:
            await update.message.reply_text("No open positions.")
            return
        now   = datetime.now(timezone.utc)
        lines = [f"📋 *{len(positions)} open*\n"]
        for p in positions:
            age = age_seconds(p["opened_at"], now)
            rem = max(0, 300 - int(age))
            d   = "UP" if p["direction"] == "YES" else "DOWN"
            lines.append(
                f"#{p['id']} {d}  ${p['size_usdc']:.2f}"
                f"  BTC=${p['btc_price_entry']:,.0f}"
                f"  ~{rem//60}m{rem%60}s"
            )
        await update.message.reply_text(
            "\n".join(lines), parse_mode=ParseMode.MARKDOWN
        )

    async def _cmd_trades(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        n = 10
        if ctx.args:
            try:
                n = max(1, min(50, int(ctx.args[0])))
            except ValueError:
                pass
        trades, s = await asyncio.to_thread(
            lambda: (
                self.ctx.engine.get_recent_trades(n),
                self.ctx.engine.get_status(),
            )
        )
        if not trades:
            await update.message.reply_text("No closed trades yet.")
            return
        total = (
            f"+${s['total_pnl']:.2f}" if s["total_pnl"] >= 0
            else f"-${abs(s['total_pnl']):.2f}"
        )
        lines = [f"📊 *Last {len(trades)} trades*  ·  total: {total}\n"]
        for t in trades:
            icon = "✅" if t["status"] == "won" else "❌"
            pnl  = t.get("pnl") or 0.0
            d    = "UP" if t["direction"] == "YES" else "DOWN"
            lines.append(f"{icon} #{t['id']} {d}  {pnl:+.2f}")
        await update.message.reply_text(
            "\n".join(lines), parse_mode=ParseMode.MARKDOWN
        )

    async def _cmd_performance(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        def _compute():
            s      = self.ctx.engine.get_status()
            closed = db.get_all_closed_trades()
            if not closed:
                return None, s
            pnls   = [t["pnl"] for t in closed if t["pnl"] is not None]
            brier  = self.ctx.learner._brier_score(closed)
            sharpe = self.ctx.learner._sharpe(pnls)
            rwr    = self.ctx.learner._rolling_win_rate(closed, 20)
            return {
                "closed": len(closed),
                "brier": brier, "sharpe": sharpe, "rwr": rwr,
                "avg_pnl": sum(pnls) / len(pnls) if pnls else 0.0,
                "best":    max(pnls)              if pnls else 0.0,
                "worst":   min(pnls)              if pnls else 0.0,
            }, s

        r, s = await asyncio.to_thread(_compute)
        if r is None:
            await update.message.reply_text("No closed trades yet.")
            return
        text = (
            f"📈 *Performance*\n\n"
            f"{r['closed']} trades  ·  {s['n_wins']}W/{s['n_losses']}L  ·  {s['win_rate']:.0%}\n"
            f"Avg: {r['avg_pnl']:+.2f}  ·  Best: {r['best']:+.2f}  ·  Worst: {r['worst']:.2f}\n"
            f"Sharpe: {r['sharpe']:.2f}  ·  Brier: {r['brier']:.3f}  ·  Recent: {r['rwr']:.0%}\n\n"
            f"💰 ${s['balance']:.2f}  ({s['return_pct']:+.1f}%)"
        )
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

    async def _cmd_learn(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        report = await asyncio.to_thread(self.ctx.learner.build_report)
        for chunk in split_message(report):
            await update.message.reply_text(chunk, parse_mode=ParseMode.MARKDOWN)

    async def _cmd_params(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        p    = await asyncio.to_thread(self.ctx.engine.get_adaptive_params)
        text = (
            f"⚙️ *Parameters*\n\n"
            f"Min edge:    `{p['min_edge']:.3f}`\n"
            f"Kelly frac:  `{p['kelly_fraction']:.2f}`\n"
            f"Max spread:  `{p['max_spread']:.2f}`\n"
            f"Max / trade: `${settings.max_position_usdc:.2f}`\n"
            f"Daily limit: `{settings.max_daily_drawdown_pct:.0%}`"
        )
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

    async def _cmd_sentiment(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        snap = self.ctx.sentiment_cache
        if snap is None:
            await update.message.reply_text("⏳ Fetching…")
            snap = await asyncio.to_thread(
                self.ctx.engine.get_sentiment_snapshot
            )
            self.ctx.sentiment_cache = snap
        if not snap or not snap.get("available"):
            await update.message.reply_text("Sentiment data unavailable.")
            return
        fg        = snap.get("fear_greed_value", 50)
        fr        = snap.get("funding_rate", 0.0)
        composite = snap.get("composite_score", 0.5)
        headlines = snap.get("headlines", [])
        hl        = "\n".join(f"  • {h}" for h in headlines) if headlines else "  No headlines"
        text = (
            f"🌡 *Sentiment*\n\n"
            f"Fear & Greed: {fg}/100\n"
            f"Funding:      {fr*100:+.2f}%\n"
            f"Composite:    {composite:.2f}\n\n"
            f"*News:*\n{hl}"
        )
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

    async def _cmd_wallets(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        for chunk in split_message(self.ctx.wallet_report_cache):
            await update.message.reply_text(chunk, parse_mode=ParseMode.MARKDOWN)

    async def _cmd_reset_start(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Confirm reset", callback_data="reset_confirm"),
            InlineKeyboardButton("❌ Cancel",         callback_data="reset_cancel"),
        ]])
        await (update.message or update.effective_message).reply_text(
            "⚠️ Reset paper balance to $1,000?",
            reply_markup=kb,
        )

    async def _cb_reset(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        if query.data == "reset_confirm":
            await asyncio.to_thread(self.ctx.engine.reset_balance, 1_000.0)
            ks = getattr(self.ctx.engine, "kill_switch", None)
            if ks:
                ks.reset(1_000.0)
            self.ctx.trading_active.set()
            if self.ctx.state_store is not None:
                self.ctx.state_store.update(enabled=True)
            await query.edit_message_text(
                "✅ Balance reset to $1,000. Trading resumed."
            )
        else:
            await query.edit_message_text("❌ Cancelled.")

    async def _cmd_live_check(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text("Checking live readiness…")

        def _check():
            try:
                from wallets.agentkit_base import (
                    live_trading_readiness_check,
                    get_all_balances,
                )
                return live_trading_readiness_check(), get_all_balances()
            except Exception as exc:
                return None, {"error": str(exc)}

        checks, balances = await asyncio.to_thread(_check)
        if checks is None:
            await update.message.reply_text(
                f"Check failed: {balances.get('error')}"
            )
            return
        yn    = lambda v: "✅" if v else "❌"
        ready = "✅ Ready" if checks.get("ready_for_live") else "⚠️ Not ready"
        text  = (
            f"🔍 *Live check* — {ready}\n\n"
            f"{yn(checks.get('clob_api_key_set'))} API keys set\n"
            f"{yn(checks.get('wallet_address_set'))} Wallet set\n"
            f"{yn(checks.get('markets_configured'))} Markets"
            f" ({checks.get('n_markets', 0)})\n"
            f"{yn(checks.get('polygon_usdc_sufficient'))} USDC ≥ $10\n\n"
            f"Base: ${balances.get('base_usdc', 0):.2f}  "
            f"Polygon: ${balances.get('polygon_usdc', 0):.2f}"
        )
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

    async def _cmd_find_markets(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        from pathlib import Path
        msg  = update.message or update.effective_message
        root = Path(__file__).resolve().parent.parent
        await msg.reply_text("🔧 Discovering BTC markets…")

        def _run():
            import json
            import subprocess
            import sys
            r = subprocess.run(
                [sys.executable, str(root / "scripts" / "find_btc_markets.py")],
                cwd=str(root), capture_output=True, text=True, timeout=90,
            )
            cfg = root / "config" / "btc_markets.json"
            n   = 0
            if cfg.exists():
                try:
                    n = len(json.load(open(cfg)))
                except Exception:
                    pass
            return r.returncode, n, (r.stdout + r.stderr)[:400]

        code, n, out = await asyncio.to_thread(_run)
        if code == 0:
            await msg.reply_text(
                f"✅ Found *{n}* market(s). Restart or wait for next cycle.",
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            await msg.reply_text(
                f"⚠️ Errors. Markets saved: {n}\n`{out}`",
                parse_mode=ParseMode.MARKDOWN,
            )

    async def _cmd_config(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        summary = await asyncio.to_thread(settings.safe_summary)
        lines   = ["⚙️ *Config* _(secrets redacted)_\n"]
        for k, v in summary.items():
            lines.append(f"`{k}`: {v}")
        await update.message.reply_text(
            "\n".join(lines), parse_mode=ParseMode.MARKDOWN
        )

    # ── Keyboard router ───────────────────────────────────────────────────────

    async def _handle_keyboard(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE
    ) -> None:
        text    = (update.message.text or "").strip()
        mapping = {
            "🚀 Start":        self._cmd_start,
            "📊 Status":       self._cmd_status,
            "📋 Positions":    self._cmd_positions,
            "📈 Trades":       self._cmd_trades,
            "📉 Performance":  self._cmd_performance,
            "🌡 Sentiment":    self._cmd_sentiment,
            "🧠 Learn":        self._cmd_learn,
            "⚙ Params":        self._cmd_params,
            "▶️ Resume":       self._cmd_resume,
            "⏸ Pause":         self._cmd_pause,
            "💊 Health":        self._cmd_health,
            "🔧 Markets":       self._cmd_find_markets,
            "🛑 Kill":          self._cmd_kill,
            "🔄 Reset":         self._cmd_reset_start,
        }
        handler = mapping.get(text)
        if handler:
            await handler(update, ctx)
        else:
            await update.message.reply_text(
                "Use the buttons or /help.", reply_markup=KEYBOARD
            )

    def _register_handlers(self) -> None:
        add = self.app.add_handler
        # Inline control panel (cb:xxx) — primary UX
        add(CallbackQueryHandler(
            self._wrap(self._cb_router), pattern="^cb:"
        ))
        add(CallbackQueryHandler(
            self._wrap(self._cb_reset), pattern="^reset_(confirm|cancel)$"
        ))
        add(CallbackQueryHandler(
            self._wrap(self._cb_kill), pattern="^kill_(confirm|cancel)$"
        ))
        add(MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            self._wrap(self._handle_keyboard),
        ))
        for cmd, handler in [
            ("start",        self._cmd_start),
            ("status",       self._cmd_status),
            ("health",       self._cmd_health),
            ("pause",        self._cmd_pause),
            ("resume",       self._cmd_resume),
            ("kill",         self._cmd_kill),
            ("positions",    self._cmd_positions),
            ("trades",       self._cmd_trades),
            ("performance",  self._cmd_performance),
            ("learn",        self._cmd_learn),
            ("params",       self._cmd_params),
            ("sentiment",    self._cmd_sentiment),
            ("wallets",      self._cmd_wallets),
            ("reset",        self._cmd_reset_start),
            ("live_check",   self._cmd_live_check),
            ("find_markets", self._cmd_find_markets),
            ("config",       self._cmd_config),
        ]:
            add(CommandHandler(cmd, self._wrap(handler)))
