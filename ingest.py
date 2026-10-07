"""On-demand download of external items and registration in the Plex library."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Callable, Optional

import httpx

from config import Settings
from providers import ProviderError, ProviderRegistry
from providers.base import find_downloaded_file
from plex_libraries import PlexLibraryError, validate_music_section

logger = logging.getLogger(__name__)

# (item_id, title) -> Plex ratingKey or None
PlexLookup = Callable[[str, Optional[str]], Optional[str]]


def title_from_filename(filename: str, item_id: str) -> str | None:
    """Recover the track title from ``"<artist> - <title> [<item_id>].<ext>"``."""
    stem = filename.rsplit(".", 1)[0]
    marker = f" [{item_id}]"
    if stem.endswith(marker):
        stem = stem[: -len(marker)]
    title = stem.split(" - ", 1)[-1].strip()
    return title or None


class IngestError(Exception):
    """Raised when an external item could not be downloaded or registered in Plex."""


class IngestConfigurationError(IngestError):
    """A non-retryable library configuration error."""


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
            from plexapi.exceptions import NotFound

            server = PlexServer(self.settings.plex_url, self.settings.plex_token, timeout=30)
            try:
                self._section = server.library.sectionByID(self.settings.music_section_id)
            except NotFound as exc:
                raise IngestConfigurationError(
                    f"MUSIC_SECTION_ID={self.settings.music_section_id} was not found. "
                    "Open General, load Plex music libraries, select a Music library and save."
                ) from exc
        try:
            validate_music_section(self._section, self.settings.music_section_id)
        except PlexLibraryError as exc:
            raise IngestConfigurationError(str(exc)) from exc
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

    def share_download_state(self, previous: "Ingestor") -> None:
        """Preserve deduplication when generations use the same Plex library and paths."""
        def identity(settings: Settings) -> tuple[str, int, Path, str]:
            return (
                settings.plex_url, settings.music_section_id,
                settings.download_dir, settings.plex_download_dir,
            )
        if identity(self.settings) == identity(previous.settings):
            self._cache = previous._cache
            self._inflight = previous._inflight

    async def wait_idle(self) -> None:
        """Keep the upstream client alive until shielded downloads have finished."""
        tasks = list(self._inflight.values())
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException):
                    logger.warning("Download failed while retiring configuration: %s", result)

    def forget(self, external_id: str) -> None:
        """Drop a cached mapping (e.g. when Plex no longer knows the ratingKey)."""
        self._cache.pop(external_id, None)

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
            def completed(job: asyncio.Task[str]) -> None:
                self._inflight.pop(external_id, None)
                if job.cancelled():
                    logger.warning("Download/registration of %s was cancelled", external_id)
                elif (error := job.exception()) is not None:
                    logger.error("Download/registration of %s failed: %s", external_id, error)
            task.add_done_callback(completed)
        # Shield so a disconnecting client does not abort a download others may await.
        rating_key = await asyncio.shield(task)
        self._cache[external_id] = rating_key
        return rating_key

    async def _ingest(self, external_id: str) -> str:
        provider, item_id = self.registry.resolve(external_id)
        if "music" not in provider.enabled_categories:
            raise IngestError(f"Music is disabled for {provider.display_name}")
        download_dir = self.settings.download_location("music")

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
        title = title or title_from_filename(path.name, item_id)

        await self.trigger_scan()
        rating_key = await self.wait_for_rating_key(item_id, title)
        logger.info("Registered %s in Plex as ratingKey %s", external_id, rating_key)
        return rating_key

    async def trigger_scan(self) -> None:
        """Request a partial scan of the download folder in the music section."""
        url = f"{self.settings.plex_url}/library/sections/{self.settings.music_section_id}/refresh"
        params = {"path": self.settings.plex_download_location("music")}
        headers = {"X-Plex-Token": self.settings.plex_token, "Accept": "application/json"}
        client = self._http or httpx.AsyncClient(timeout=30)
        try:
            response = await client.get(url, params=params, headers=headers, timeout=30.0)
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
            except IngestConfigurationError:
                raise
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
