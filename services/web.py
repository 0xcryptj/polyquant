"""
WebService — optional FastAPI dashboard running inside the asyncio event loop.

Uses uvicorn.Server.serve() so it runs in the same process and event loop.
No subprocess, no port conflicts, clean lifecycle via server.should_exit.

Enable with: python app.py  (--no-web is NOT passed)
"""
from __future__ import annotations

import asyncio

import structlog
import uvicorn

from runtime.context import RuntimeContext
from services.base import BaseService, HealthStatus

log = structlog.get_logger("web")


class WebService(BaseService):
    name = "web"

    def __init__(self, ctx: RuntimeContext, port: int = 8080) -> None:
        super().__init__(ctx)
        self.port     = port
        self._task:   asyncio.Task | None   = None
        self._server: uvicorn.Server | None = None

    async def start(self) -> None:
        from web.app import create_app      # local import: web/ is optional

        app    = create_app(self.ctx)
        config = uvicorn.Config(
            app,
            host="0.0.0.0",
            port=self.port,
            log_level="warning",
            access_log=False,
        )
        self._server = uvicorn.Server(config)
        self._task   = asyncio.create_task(
            self._server.serve(), name="web-server"
        )
        self._mark_started()
        log.info("started", port=self.port)

    async def stop(self) -> None:
        if self._server:
            self._server.should_exit = True
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=10.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
        log.info("stopped")

    async def health(self) -> HealthStatus:
        if self._task and self._task.done():
            return self._health_fail("server task ended unexpectedly")
        return self._health_ok(port=self.port)
