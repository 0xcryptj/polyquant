"""
Orchestrator — manages service lifecycle, signal handling, and graceful shutdown.

Startup order:  DataService → TradingService → SupervisorService
                → TelegramService (opt) → WebService (opt)

Shutdown order: reverse (WebService first, DataService last).
Each service gets _STOP_TIMEOUT seconds before the orchestrator moves on.

Provider bootstrap (synchronous, before any service starts):
  1. TradeBlotter (audit log)
  2. WalletProvider (sdk | agentic | none)
  3. ExecutionProvider (clob | cli)
  4. PaperEngine  (receives blotter reference)
  5. Learner
"""
from __future__ import annotations

import asyncio
import signal
import sys
from typing import List

import structlog

from config.settings import settings
from paper_trading.engine import PaperEngine, STARTING_PAPER_BALANCE
from paper_trading.learner import Learner
from paper_trading import persistence as db
from runtime.context import RuntimeContext
from services.base import BaseService
from services.data import DataService
from services.trading import TradingService
from services.supervisor import SupervisorService

log = structlog.get_logger("orchestrator")

_STOP_TIMEOUT    = 30   # seconds per service on graceful shutdown
_HEALTH_INTERVAL = 60   # seconds between orchestrator health sweeps


class Orchestrator:
    def __init__(
        self,
        enable_telegram:    bool = True,
        enable_web:         bool = False,
        wallet_provider:    str | None = None,
        execution_provider: str | None = None,
    ) -> None:
        self.enable_telegram    = enable_telegram
        self.enable_web         = enable_web
        self._wallet_prov_name  = wallet_provider    or settings.wallet_provider
        self._exec_prov_name    = execution_provider or settings.execution_provider
        self.ctx                = RuntimeContext()
        self._services: List[BaseService] = []

    # ── Public ────────────────────────────────────────────────────────────────

    async def run(self) -> None:
        log.info("boot",
                 version="2.0.0",
                 paper=settings.paper_trading,
                 wallet_provider=self._wallet_prov_name,
                 execution_provider=self._exec_prov_name,
                 telegram=self.enable_telegram,
                 web=self.enable_web)

        # ── 1. Audit log (blotter) ────────────────────────────────────────────
        from paper_trading.blotter import TradeBlotter
        self.ctx.blotter = TradeBlotter()
        log.info("blotter.ready", path=self.ctx.blotter.path())

        # ── 2. Providers (synchronous, before any async service) ──────────────
        self._bootstrap_providers()

        # ── 3. Initialise DB and shared domain objects ────────────────────────
        db.init_db()
        engine             = PaperEngine(starting_balance=STARTING_PAPER_BALANCE)
        engine.blotter     = self.ctx.blotter
        engine.execution_provider = self.ctx.execution_provider
        self.ctx.engine    = engine
        self.ctx.learner   = Learner()

        # ── Install OS signal handlers ────────────────────────────────────────
        self._install_signal_handlers()

        # ── Build service list ────────────────────────────────────────────────
        self._services = self._build_services()

        # ── Start services in order ───────────────────────────────────────────
        for svc in self._services:
            try:
                log.info("service.starting", service=svc.name)
                await svc.start()
                log.info("service.started", service=svc.name)
            except Exception as exc:
                log.critical("service.start_failed",
                             service=svc.name, error=str(exc))
                await self._shutdown(exit_code=1)
                return

        log.info("runtime.ready",
                 services=[s.name for s in self._services])

        # ── Main monitor loop (health checks + shutdown wait) ─────────────────
        try:
            await self._monitor_loop()
        finally:
            await self._shutdown(exit_code=0)

    # ── Private ───────────────────────────────────────────────────────────────

    def _bootstrap_providers(self) -> None:
        """
        Initialise WalletProvider and ExecutionProvider synchronously.

        Failures are non-fatal in paper mode but fatal in live mode
        (paper_trading=false).
        """
        # ── Wallet provider ───────────────────────────────────────────────────
        wp_name = (self._wallet_prov_name or "").lower()
        if wp_name in ("none", ""):
            log.info("wallet_provider.skipped")
        elif wp_name == "agentic":
            try:
                from wallets.providers.coinbase_agentic import CoinbaseAgenticProvider
                self.ctx.wallet_provider = CoinbaseAgenticProvider()
                log.info("wallet_provider.ready", provider="agentic")
            except Exception as exc:
                self._provider_error("wallet", "agentic", exc)
        else:
            # Default: "sdk"
            try:
                from wallets.providers.clob_wallet import ClobWalletProvider
                self.ctx.wallet_provider = ClobWalletProvider()
                log.info("wallet_provider.ready", provider="sdk")
            except Exception as exc:
                self._provider_error("wallet", "sdk", exc)

        # ── Execution provider ────────────────────────────────────────────────
        ep_name = (self._exec_prov_name or "clob").lower()
        if ep_name == "cli":
            try:
                from execution.providers.polymarket_cli import PolymarketCLIProvider
                self.ctx.execution_provider = PolymarketCLIProvider()
                log.info("execution_provider.ready", provider="cli")
            except Exception as exc:
                self._provider_error("execution", "cli", exc)
        else:
            # Default: "clob"
            try:
                clob_client = None
                if self.ctx.wallet_provider is not None:
                    # Reuse the CLOB client already initialised by sdk wallet
                    try:
                        bundle      = self.ctx.wallet_provider._get_bundle()  # type: ignore[attr-defined]
                        clob_client = bundle.clob_client
                    except AttributeError:
                        pass
                from execution.providers.clob import ClobExecutionProvider
                self.ctx.execution_provider = ClobExecutionProvider(clob_client=clob_client)
                log.info("execution_provider.ready", provider="clob")
            except Exception as exc:
                self._provider_error("execution", "clob", exc)

    def _provider_error(self, kind: str, name: str, exc: Exception) -> None:
        if settings.paper_trading:
            log.warning(
                f"{kind}_provider.init_failed (non-fatal in paper mode)",
                provider=name, error=str(exc),
            )
        else:
            log.critical(
                f"{kind}_provider.init_failed (FATAL in live mode)",
                provider=name, error=str(exc),
            )
            sys.exit(1)

    def _build_services(self) -> List[BaseService]:
        svcs: List[BaseService] = [
            DataService(self.ctx),
            TradingService(self.ctx),
            SupervisorService(self.ctx),
        ]
        if self.enable_telegram:
            from services.telegram import TelegramService
            svcs.append(TelegramService(self.ctx))
        if self.enable_web:
            from services.web import WebService
            svcs.append(WebService(self.ctx))
        return svcs

    async def _monitor_loop(self) -> None:
        """Wait for shutdown; run health checks every _HEALTH_INTERVAL seconds."""
        while not self.ctx.shutdown_event.is_set():
            try:
                await asyncio.wait_for(
                    self.ctx.shutdown_event.wait(),
                    timeout=_HEALTH_INTERVAL,
                )
            except asyncio.TimeoutError:
                await self._run_health_checks()

    async def _run_health_checks(self) -> None:
        for svc in self._services:
            try:
                h = await asyncio.wait_for(svc.health(), timeout=10.0)
                if not h.healthy:
                    log.warning("service.unhealthy",
                                service=svc.name,
                                details=h.details)
            except asyncio.TimeoutError:
                log.error("service.health_timeout", service=svc.name)
            except Exception as exc:
                log.error("service.health_error",
                          service=svc.name, error=str(exc))

    async def _shutdown(self, exit_code: int = 0) -> None:
        log.info("shutdown.begin", exit_code=exit_code)

        # Signal all loops to stop
        self.ctx.shutdown_event.set()
        self.ctx.trading_active.clear()

        # Stop services in reverse startup order
        for svc in reversed(self._services):
            try:
                log.info("service.stopping", service=svc.name)
                await asyncio.wait_for(svc.stop(), timeout=_STOP_TIMEOUT)
                log.info("service.stopped", service=svc.name)
            except asyncio.TimeoutError:
                log.error("service.stop_timeout", service=svc.name)
            except Exception as exc:
                log.error("service.stop_error",
                          service=svc.name, error=str(exc))

        log.info("shutdown.complete")
        sys.exit(exit_code)

    def _install_signal_handlers(self) -> None:
        """Register SIGINT and SIGTERM to trigger graceful shutdown."""
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            return

        def _handle(signame: str) -> None:
            log.info("signal.received", signal=signame)
            loop.call_soon_threadsafe(self.ctx.shutdown_event.set)

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                # Preferred: event-loop-safe handler (Unix)
                loop.add_signal_handler(
                    sig, lambda s=sig.name: _handle(s)
                )
            except (NotImplementedError, OSError):
                # Fallback: threading signal handler (Windows)
                signal.signal(
                    sig, lambda s, f, n=sig.name: _handle(n)
                )
