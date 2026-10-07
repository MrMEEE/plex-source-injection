"""Request-scoped runtime generations for non-disruptive configuration updates."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path

import httpx
from starlette.types import ASGIApp, Receive, Scope, Send

from cleanup import run_cleanup_loop
from config import Settings
from ingest import Ingestor
from providers import ProviderRegistry

logger = logging.getLogger(__name__)


class Runtime:
    def __init__(
        self, settings: Settings, registry: ProviderRegistry,
        transport: httpx.AsyncBaseTransport | None = None, ingestor: Ingestor | None = None,
    ) -> None:
        self.settings = settings
        self.registry = registry
        self.http = httpx.AsyncClient(
            base_url=settings.plex_url, timeout=httpx.Timeout(None, connect=10.0),
            transport=transport, follow_redirects=False,
        )
        self.ingestor = ingestor or Ingestor(settings, registry, http_client=self.http)
        self.auth_cache: dict[str, float] = {}
        self.cleanup_task: asyncio.Task[None] | None = None
        self.requests = 0
        self.idle = asyncio.Event()
        self.idle.set()

    def start_cleanup(self, enabled: bool) -> None:
        if enabled and self.settings.retention_days > 0:
            self.cleanup_task = asyncio.create_task(run_cleanup_loop(
                self.settings.download_dir, self.settings.retention_days,
                self.settings.cleanup_interval_hours * 3600, on_deleted=self.on_deleted,
            ))

    async def on_deleted(self, paths: list[Path]) -> None:
        self.ingestor.clear_cache()
        try:
            await self.ingestor.trigger_scan()
        except Exception:
            logger.exception("Plex rescan after cleanup failed")

    async def stop_cleanup(self) -> None:
        if self.cleanup_task is not None:
            self.cleanup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.cleanup_task

    async def close(self) -> None:
        await self.idle.wait()
        await self.ingestor.wait_idle()
        await self.http.aclose()


class RuntimeMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"].rstrip("/") == "/admin" or scope["path"].startswith("/admin/"):
            await self.app(scope, receive, send)
            return
        runtime: Runtime = scope["app"].state.runtime
        scope.setdefault("state", {})["runtime"] = runtime
        runtime.requests += 1
        runtime.idle.clear()
        try:
            await self.app(scope, receive, send)
        finally:
            runtime.requests -= 1
            if runtime.requests == 0:
                runtime.idle.set()
