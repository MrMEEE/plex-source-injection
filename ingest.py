"""On-demand download of external items and registration in the Plex library."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Optional

import httpx

from config import Settings
from providers import ProviderError, ProviderRegistry
from providers.base import find_downloaded_file

logger = logging.getLogger(__name__)

# (item_id, title) -> Plex ratingKey or None
PlexLookup = Callable[[str, Optional[str]], Optional[str]]


class IngestError(Exception):
    """Raised when an external item could not be downloaded or registered in Plex."""


class ItemNotFoundError(LookupError):
    """Raised when the provider reports that the requested item does not exist."""


class PlexApiLookup:
    """Find tracks in the Plex music section by the ``[<item_id>]`` filename marker."""

    RECENT_LIMIT = 100

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._section: Any = None

    def _get_section(self) -> Any:
        if self._section is None:
            from plexapi.server import PlexServer

            server = PlexServer(self.settings.plex_url, self.settings.plex_token, timeout=30)
            self._section = server.library.sectionByID(self.settings.music_section_id)
        return self._section

    def __call__(self, item_id: str, title: str | None) -> str | None:
        section = self._get_section()
        candidates = list(
            section.search(libtype="track", sort="addedAt:desc", maxresults=self.RECENT_LIMIT)
        )
        if title:
            candidates.extend(section.search(title=title, libtype="track"))
        marker = f"[{item_id}]"
        for track in candidates:
            for media in getattr(track, "media", None) or []:
                for part in getattr(media, "parts", None) or []:
                    if marker in (getattr(part, "file", "") or ""):
                        return str(track.ratingKey)
        return None


class Ingestor:
    """Coordinates provider downloads, Plex scans and ratingKey resolution."""

    def __init__(
        self,
        settings: Settings,
        registry: ProviderRegistry,
        http_client: httpx.AsyncClient | None = None,
        plex_lookup: PlexLookup | None = None,
    ) -> None:
        self.settings = settings
        self.registry = registry
        self._http = http_client
        self._lookup = plex_lookup or PlexApiLookup(settings)
        self._cache: dict[str, str] = {}
        self._inflight: dict[str, asyncio.Task[str]] = {}

    def clear_cache(self) -> None:
        self._cache.clear()

    async def download_and_register(self, external_id: str) -> str:
        """Ensure ``external_id`` exists in Plex and return its real numeric ratingKey.

        Concurrent calls for the same ID share a single download job.
        """
        cached = self._cache.get(external_id)
        if cached:
            return cached
        task = self._inflight.get(external_id)
        if task is None:
            task = asyncio.create_task(self._ingest(external_id))
            self._inflight[external_id] = task
            task.add_done_callback(lambda _t: self._inflight.pop(external_id, None))
        # Shield so a disconnecting client does not abort a download others may await.
        rating_key = await asyncio.shield(task)
        self._cache[external_id] = rating_key
        return rating_key

    async def _ingest(self, external_id: str) -> str:
        provider, item_id = self.registry.resolve(external_id)
        download_dir = self.settings.download_dir

        title: str | None = None
        try:
            metadata = await asyncio.wait_for(
                provider.fetch_metadata(item_id), timeout=self.settings.provider_timeout
            )
        except Exception:
            logger.warning("fetch_metadata failed for %s; continuing", external_id, exc_info=True)
        else:
            if metadata is None:
                raise ItemNotFoundError(f"{provider.display_name} item {item_id} not found")
            title = metadata.title

        existing = await asyncio.to_thread(self._lookup, item_id, title)
        if existing:
            logger.info("%s already indexed as ratingKey %s", external_id, existing)
            return existing

        path = find_downloaded_file(download_dir, item_id)
        if path is None:
            logger.info("Downloading %s via %s", external_id, provider.name)
            try:
                path = await asyncio.wait_for(
                    provider.download(item_id, download_dir),
                    timeout=self.settings.download_timeout,
                )
            except asyncio.TimeoutError as exc:
                raise IngestError(f"Download of {external_id} timed out") from exc
            except ProviderError as exc:
                raise IngestError(str(exc)) from exc
        logger.info("Downloaded %s to %s", external_id, path)

        await self.trigger_scan()
        return await self.wait_for_rating_key(item_id, title)

    async def trigger_scan(self) -> None:
        """Request a partial scan of the download folder in the music section."""
        url = f"{self.settings.plex_url}/library/sections/{self.settings.music_section_id}/refresh"
        params = {"path": self.settings.plex_download_dir}
        headers = {"X-Plex-Token": self.settings.plex_token, "Accept": "application/json"}
        client = self._http or httpx.AsyncClient(timeout=30)
        try:
            response = await client.get(url, params=params, headers=headers)
        except httpx.HTTPError as exc:
            raise IngestError(f"Plex scan request failed: {exc}") from exc
        finally:
            if self._http is None:
                await client.aclose()
        if response.status_code >= 400:
            raise IngestError(f"Plex scan request returned HTTP {response.status_code}")

    async def wait_for_rating_key(self, item_id: str, title: str | None) -> str:
        """Poll Plex until a track tagged with ``[item_id]`` appears."""
        deadline = time.monotonic() + self.settings.scan_timeout
        while True:
            try:
                rating_key = await asyncio.to_thread(self._lookup, item_id, title)
            except Exception:
                logger.warning("Plex lookup failed while polling for %s", item_id, exc_info=True)
                rating_key = None
            if rating_key:
                return rating_key
            if time.monotonic() >= deadline:
                raise IngestError(
                    f"Timed out after {self.settings.scan_timeout}s waiting for Plex to index {item_id}"
                )
            await asyncio.sleep(self.settings.scan_poll_interval)
