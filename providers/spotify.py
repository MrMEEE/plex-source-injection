"""Spotify provider: spotipy for search/metadata, the spotdl CLI for downloads."""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
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
        def _search() -> list[ExternalTrack]:
            result = self._spotify().search(q=query, type="track", limit=min(max(limit, 1), 50))
            items = ((result or {}).get("tracks") or {}).get("items") or []
            return [t for t in (self._to_track(i) for i in items if i) if t]

        return await asyncio.to_thread(_search)

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
