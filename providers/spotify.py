"""Spotify provider: spotipy for search/metadata, the spotdl CLI for downloads."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
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
WATCH_URL = "https://www.youtube.com/watch?v={id}"
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
            "SPOTDL_PROVIDER_CREDENTIALS", "Give spotdl these credentials",
            "Passed through a private temporary config file, never the command line, and makes spotdl "
            "use the official Spotify API instead of its keyless, rate-limited web client. Off uses "
            "spotdl's own config.json.",
            kind="switch", default="true",
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
        self.provider_credentials = (settings.get("SPOTDL_PROVIDER_CREDENTIALS", "true") or "").lower() in (
            "1",
            "true",
            "yes",
        )
        self._client: Any = None
        self.check_availability = settings.get("SPOTIFY_CHECK_AVAILABILITY", "true").lower() in ("true", "1", "yes")
        # Spotify track id -> (recheck after, matched YouTube video id or None). Entries stay
        # until evicted so downloads can reuse the match after the recheck time has passed.
        self._availability: OrderedDict[str, tuple[float, str | None]] = OrderedDict()
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
                return cached[1] is not None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.warning("Spotify source check budget exhausted for %s; hiding result", track.item_id)
                return False
            try:
                async with asyncio.timeout(remaining):
                    async with self._availability_slots:
                        video_id = await self._source_available(track)
            except TimeoutError:
                logger.warning("Spotify source check timed out for %s; hiding result", track.item_id)
                return False
            except ProviderError as exc:
                logger.warning("Spotify source check failed for %s; hiding result: %s", track.item_id, exc)
                return False
            self._availability[track.item_id] = (time.monotonic() + (300 if video_id else 60), video_id)
            self._availability.move_to_end(track.item_id)
            while len(self._availability) > 500:
                self._availability.popitem(last=False)
            if video_id is None:
                logger.info("Spotify track %s has no confirmed audio source; hiding result", track.item_id)
            return video_id is not None

        confirmed = await asyncio.gather(*(check(track) for track in tracks))
        return [track for track, available in zip(tracks, confirmed) if available]

    async def _source_available(self, track: ExternalTrack) -> str | None:
        """Quick heuristic: return the first YouTube video id for "artist - title", if any.

        spotdl's own matching takes 15-35 seconds per track, far too slow for a search
        response. The match is reused for the download, skipping spotdl's matching.
        """
        if self.ytdlp_error:
            raise ProviderError(self.ytdlp_error)
        query = f"ytsearch1:{track.artist} - {track.title}"
        if self.ytdlp_binary is None:
            found = await asyncio.to_thread(self._youtube_search_module, query)
        else:
            found = await self._youtube_search_cli(query)
        if found is None:
            logger.info("No YouTube match for Spotify track %s (%s - %s)", track.item_id, track.artist, track.title)
        return found

    async def _youtube_search_cli(self, query: str) -> str | None:
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
        return next(
            (line.strip() for line in output.decode(errors="replace").splitlines() if YOUTUBE_ID_RE.match(line.strip())),
            None,
        )

    @staticmethod
    def _youtube_search_module(query: str) -> str | None:
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
        return next(
            (entry["id"] for entry in (info or {}).get("entries") or []
             if entry and YOUTUBE_ID_RE.match(str(entry.get("id") or ""))),
            None,
        )

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

    def _song_record(self, item_id: str, video_id: str) -> dict[str, Any]:
        """Build spotdl's full song record (as ``Song.from_url`` would) with our own client.

        Handing spotdl a complete record in a ``.spotdl`` file skips its own Spotify
        lookups, which go through a shared, rate-limited client and take seconds.
        """
        client = self._spotify()
        track = client.track(item_id)
        if not track or not track.get("artists") or not track.get("album"):
            raise ProviderError(f"Spotify returned no usable metadata for {item_id}")
        artist_id = track["artists"][0].get("id")
        album_id = track["album"]["id"]
        album = client.album(album_id) or track["album"]
        artist = (client.artist(artist_id) if artist_id else None) or {}
        release_date = str(album.get("release_date") or "")
        album_tracks = ((album.get("tracks") or {}).get("items")) or []
        images = [i for i in album.get("images") or [] if i.get("url")]
        copyrights = album.get("copyrights") or []
        return {
            "name": track["name"],
            "artists": [a["name"] for a in track["artists"]],
            "artist": track["artists"][0]["name"],
            "artist_id": artist_id,
            "genres": list(album.get("genres") or []) + list(artist.get("genres") or []),
            "disc_number": track.get("disc_number") or 1,
            "disc_count": int(album_tracks[-1].get("disc_number") or 1) if album_tracks else track.get("disc_number") or 1,
            "album_name": album.get("name") or "",
            "album_artist": ((album.get("artists") or track["artists"])[0]).get("name") or "",
            "album_id": album_id,
            "album_type": album.get("album_type"),
            "duration": int((track.get("duration_ms") or 0) / 1000),
            "year": int(release_date[:4]) if release_date[:4].isdigit() else 0,
            "date": release_date,
            "track_number": track.get("track_number") or 1,
            "tracks_count": album.get("total_tracks") or track["album"].get("total_tracks") or 1,
            "song_id": track["id"],
            "explicit": bool(track.get("explicit")),
            "publisher": album.get("label") or "",
            "url": (track.get("external_urls") or {}).get("spotify") or TRACK_URL.format(id=item_id),
            # spotdl's tagger crashes on a null ISRC; an empty one is skipped.
            "isrc": (track.get("external_ids") or {}).get("isrc") or "",
            "cover_url": max(images, key=lambda i: (i.get("width") or 0) * (i.get("height") or 0))["url"] if images else None,
            "copyright_text": copyrights[0].get("text") if copyrights else None,
            "download_url": WATCH_URL.format(id=video_id),
            "popularity": track.get("popularity"),
        }

    def _private_config_env(self, home: Path) -> dict[str, str]:
        """Environment where spotdl loads our credentials from a private config.json.

        spotdl only reads ``~/.config/spotdl/config.json``, so HOME points at a 0700 temporary
        directory. The real cache directory is kept so yt-dlp's caches survive between runs.
        """
        env = tool_environment()
        real_home = env.get("HOME") or str(Path.home())
        env.setdefault("XDG_CACHE_HOME", str(Path(real_home) / ".cache"))
        env["HOME"] = str(home)
        config_dir = home / ".config" / "spotdl"
        config_dir.mkdir(parents=True, mode=0o700)
        fd = os.open(config_dir / "config.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as config:
            json.dump(
                {
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    # spotdl 4.5+ otherwise uses a keyless web client that ignores credentials.
                    "use_official_api": True,
                    "load_config": True,
                },
                config,
            )
        return env

    async def download(self, item_id: str, output_dir: Path) -> Path:
        if not SPOTIFY_ID_RE.match(item_id):
            raise ProviderError(f"Invalid Spotify track id: {item_id!r}")
        if self.dependency_error:
            raise ProviderError(self.dependency_error)
        binary = self.managed_binary or shutil.which(self.spotdl_binary)
        if binary is None:
            raise ProviderError(f"spotdl executable {self.spotdl_binary!r} not found in PATH")
        output_dir.mkdir(parents=True, exist_ok=True)
        query = TRACK_URL.format(id=item_id)
        record: dict[str, Any] | None = None
        matched = self._availability.get(item_id)
        if matched and matched[1]:
            logger.info("Reusing YouTube match %s for Spotify track %s", matched[1], item_id)
            try:
                record = await asyncio.to_thread(self._song_record, item_id, matched[1])
            except Exception as exc:  # noqa: BLE001 - fall back to letting spotdl fetch metadata
                logger.warning("Could not prepare Spotify metadata for %s; spotdl will fetch it: %s", item_id, exc)
                # spotdl's "YouTubeURL|SpotifyURL" form downloads that video with Spotify metadata.
                query = f"{WATCH_URL.format(id=matched[1])}|{query}"
        with tempfile.TemporaryDirectory(prefix="spotdl-") as home:
            if record is not None:
                song_file = Path(home) / f"{item_id}.spotdl"
                song_file.write_text(json.dumps([record]), encoding="utf-8")
                query = str(song_file)
            args = [
                binary,
                "download",
                query,
                "--output",
                str(output_dir / OUTPUT_TEMPLATE),
                "--format",
                self.settings.audio_format,
            ]
            if self.ffmpeg_path:
                args += ["--ffmpeg", self.ffmpeg_path]
            # An empty provider list skips lyrics lookups, which add seconds and mostly fail.
            args.append("--lyrics")
            env = self._private_config_env(Path(home)) if self.provider_credentials else tool_environment()
            process = await asyncio.create_subprocess_exec(
                *args, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
            )
            try:
                output, _ = await asyncio.wait_for(
                    process.communicate(), timeout=self.settings.download_timeout
                )
            except BaseException as exc:
                if process.returncode is None:
                    process.kill()
                await process.wait()
                if isinstance(exc, asyncio.TimeoutError):
                    raise ProviderError(f"spotdl timed out downloading {item_id}") from exc
                raise
        if process.returncode != 0:
            tail = output.decode(errors="replace")[-500:] if output else ""
            raise ProviderError(f"spotdl exited with {process.returncode}: {tail}")
        path = find_downloaded_file(output_dir, item_id)
        if path is None:
            tail = output.decode(errors="replace")[-2000:].strip() if output else "No output from spotdl"
            raise ProviderError(f"spotdl finished but no file found for {item_id}. spotdl output: {tail}")
        return path
