"""Spotify provider: spotipy for search/metadata, the spotdl CLI for downloads."""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

from dependencies import DependencyError, ffmpeg_location, resolve_tool, tool_environment
from .base import (
    BaseProvider,
    ExternalTrack,
    ProviderConfigurationError,
    ProviderError,
    ProviderSetting,
    find_downloaded_file,
)
from .registry import register_provider

logger = logging.getLogger(__name__)

TRACK_URL = "https://open.spotify.com/track/{id}"
OUTPUT_TEMPLATE = "{artist} - {title} [{track-id}].{output-ext}"
SPOTIFY_ID_RE = re.compile(r"^[A-Za-z0-9]{22}$")
YOUTUBE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


@register_provider
class SpotifyProvider(BaseProvider):
    name = "spotify"
    prefix = "sp"
    display_name = "Spotify"
    description = "Search Spotify with your app credentials and download audio with spotdl."
    dependencies = ("spotdl", "ffmpeg")
    config_fields = (
        ProviderSetting("SPOTIFY_CLIENT_ID", "Client ID", "Required. From your Spotify developer application."),
        ProviderSetting("SPOTIFY_CLIENT_SECRET", "Client secret", "Required. From your Spotify developer application."),
        ProviderSetting("SPOTDL_BINARY", "spotdl executable", "Executable name or absolute path.", default="spotdl"),
        ProviderSetting(
            "SPOTIFY_CHECK_AVAILABILITY", "Hide unconfirmed audio sources",
            "Quickly search YouTube for each Spotify result and hide tracks with no match. Slow or failed checks are hidden; results are cached briefly.",
            kind="switch", default="true",
        ),
        ProviderSetting(
            "SPOTDL_PASS_CREDENTIALS", "Pass credentials to spotdl",
            "Exposes Spotify credentials in the process list. Off uses spotdl's own config.json.",
            kind="switch", default="false",
        ),
    )

    def __init__(self, settings: Any) -> None:
        super().__init__(settings)
        self.client_id = settings.get("SPOTIFY_CLIENT_ID")
        self.client_secret = settings.get("SPOTIFY_CLIENT_SECRET")
        if not self.client_id or not self.client_secret:
            raise ProviderConfigurationError("SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET not set")
        self.spotdl_binary = settings.get("SPOTDL_BINARY", "spotdl")
        self.managed_binary: str | None = None
        self.dependency_error: str | None = None
        try:
            if settings.get("SPOTDL_MODE", "external") in ("managed", "managed-python"):
                self.managed_binary = resolve_tool(settings, "spotdl")
            self.ffmpeg_path = ffmpeg_location(settings)
        except DependencyError as exc:
            self.dependency_error = str(exc)
            self.ffmpeg_path = None
        # Passing credentials on the command line exposes them in the process list, so it is
        # opt-in; by default spotdl uses the credentials from its own config.json.
        self.pass_credentials = (settings.get("SPOTDL_PASS_CREDENTIALS", "false") or "").lower() in (
            "1",
            "true",
            "yes",
        )
        self._client: Any = None
        self.check_availability = settings.get("SPOTIFY_CHECK_AVAILABILITY", "true").lower() in ("true", "1", "yes")
        self._availability: OrderedDict[str, tuple[float, bool]] = OrderedDict()
        self._availability_slots = asyncio.Semaphore(3)
        self.ytdlp_binary: str | None = None
        self.ytdlp_error: str | None = None
        if self.check_availability and settings.get("YTDLP_MODE", "bundled") != "bundled":
            try:
                self.ytdlp_binary = resolve_tool(settings, "yt-dlp")
            except DependencyError as exc:
                self.ytdlp_error = str(exc)

    def _spotify(self) -> Any:
        if self._client is None:
            import spotipy
            from spotipy.oauth2 import SpotifyClientCredentials

            self._client = spotipy.Spotify(
                auth_manager=SpotifyClientCredentials(
                    client_id=self.client_id, client_secret=self.client_secret
                ),
                requests_timeout=self.settings.provider_timeout,
                retries=0,
            )
        return self._client

    def _to_track(self, item: dict[str, Any]) -> ExternalTrack | None:
        track_id = item.get("id")
        if not track_id:
            return None
        album = item.get("album") or {}
        images = album.get("images") or []
        artists = ", ".join(a["name"] for a in item.get("artists") or [] if a.get("name"))
        return ExternalTrack(
            provider=self.name,
            item_id=track_id,
            title=item.get("name") or track_id,
            artist=artists or "Unknown Artist",
            album=album.get("name"),
            duration_ms=item.get("duration_ms"),
            thumb=images[0]["url"] if images else None,
            url=TRACK_URL.format(id=track_id),
        )

    async def search(self, query: str, limit: int) -> list[ExternalTrack]:
        deadline = time.monotonic() + self.settings.provider_timeout * 0.9
        def _search() -> list[ExternalTrack]:
            result = self._spotify().search(q=query, type="track", limit=min(max(limit, 1), 50))
            items = ((result or {}).get("tracks") or {}).get("items") or []
            return [t for t in (self._to_track(i) for i in items if i) if t]

        tracks = await asyncio.to_thread(_search)
        if not self.check_availability:
            return tracks

        async def check(track: ExternalTrack) -> bool:
            cached = self._availability.get(track.item_id)
            if cached and cached[0] > time.monotonic():
                self._availability.move_to_end(track.item_id)
                return cached[1]
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.warning("Spotify source check budget exhausted for %s; hiding result", track.item_id)
                return False
            try:
                async with asyncio.timeout(remaining):
                    async with self._availability_slots:
                        available = await self._source_available(track)
            except TimeoutError:
                logger.warning("Spotify source check timed out for %s; hiding result", track.item_id)
                return False
            except ProviderError as exc:
                logger.warning("Spotify source check failed for %s; hiding result: %s", track.item_id, exc)
                return False
            self._availability[track.item_id] = (time.monotonic() + (300 if available else 60), available)
            self._availability.move_to_end(track.item_id)
            while len(self._availability) > 500:
                self._availability.popitem(last=False)
            if not available:
                logger.info("Spotify track %s has no confirmed audio source; hiding result", track.item_id)
            return available

        confirmed = await asyncio.gather(*(check(track) for track in tracks))
        return [track for track, available in zip(tracks, confirmed) if available]

    async def _source_available(self, track: ExternalTrack) -> bool:
        """Quick heuristic: a YouTube search for "artist - title" returns at least one video.

        spotdl's own matching takes 15-35 seconds per track, far too slow for a search
        response, so a match here does not guarantee that spotdl will accept it later.
        """
        if self.ytdlp_error:
            raise ProviderError(self.ytdlp_error)
        query = f"ytsearch1:{track.artist} - {track.title}"
        if self.ytdlp_binary is None:
            found = await asyncio.to_thread(self._youtube_search_module, query)
        else:
            found = await self._youtube_search_cli(query)
        if not found:
            logger.info("No YouTube match for Spotify track %s (%s - %s)", track.item_id, track.artist, track.title)
        return found

    async def _youtube_search_cli(self, query: str) -> bool:
        args = [self.ytdlp_binary, "--ignore-config", "--no-warnings", "--flat-playlist", "--print", "id", query]
        try:
            process = await asyncio.create_subprocess_exec(
                *args, env=tool_environment(),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise ProviderError(f"Cannot start yt-dlp source check: {exc.strerror}") from exc
        try:
            output, errors = await process.communicate()
        except asyncio.CancelledError:
            if process.returncode is None:
                process.kill()
            await process.wait()
            raise
        if process.returncode:
            raise ProviderError(f"yt-dlp source check exited with {process.returncode}: {errors.decode(errors='replace')[-1000:]}")
        return any(YOUTUBE_ID_RE.match(line.strip()) for line in output.decode(errors="replace").splitlines())

    @staticmethod
    def _youtube_search_module(query: str) -> bool:
        try:
            import yt_dlp
        except ImportError as exc:
            raise ProviderError("yt-dlp is not installed for Spotify source checks") from exc
        opts = {"quiet": True, "no_warnings": True, "extract_flat": "in_playlist", "skip_download": True}
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(query, download=False)
        except Exception as exc:
            raise ProviderError(f"yt-dlp source check failed: {exc}") from exc
        return any(entry and entry.get("id") for entry in (info or {}).get("entries") or [])

    async def fetch_metadata(self, item_id: str) -> ExternalTrack | None:
        if not SPOTIFY_ID_RE.match(item_id):
            return None

        def _fetch() -> ExternalTrack | None:
            import spotipy

            try:
                return self._to_track(self._spotify().track(item_id))
            except spotipy.SpotifyException:
                return None

        return await asyncio.to_thread(_fetch)

    async def download(self, item_id: str, output_dir: Path) -> Path:
        if not SPOTIFY_ID_RE.match(item_id):
            raise ProviderError(f"Invalid Spotify track id: {item_id!r}")
        if self.dependency_error:
            raise ProviderError(self.dependency_error)
        binary = self.managed_binary or shutil.which(self.spotdl_binary)
        if binary is None:
            raise ProviderError(f"spotdl executable {self.spotdl_binary!r} not found in PATH")
        output_dir.mkdir(parents=True, exist_ok=True)
        args = [
            binary,
            "download",
            TRACK_URL.format(id=item_id),
            "--output",
            str(output_dir / OUTPUT_TEMPLATE),
            "--format",
            self.settings.audio_format,
        ]
        if self.pass_credentials:
            args += ["--client-id", self.client_id, "--client-secret", self.client_secret]
        if self.ffmpeg_path:
            args += ["--ffmpeg", self.ffmpeg_path]
        process = await asyncio.create_subprocess_exec(
            *args, env=tool_environment(), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        try:
            output, _ = await asyncio.wait_for(
                process.communicate(), timeout=self.settings.download_timeout
            )
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.wait()
            raise ProviderError(f"spotdl timed out downloading {item_id}") from exc
        if process.returncode != 0:
            tail = output.decode(errors="replace")[-500:] if output else ""
            raise ProviderError(f"spotdl exited with {process.returncode}: {tail}")
        path = find_downloaded_file(output_dir, item_id)
        if path is None:
            tail = output.decode(errors="replace")[-2000:].strip() if output else "No output from spotdl"
            raise ProviderError(f"spotdl finished but no file found for {item_id}. spotdl output: {tail}")
        return path
