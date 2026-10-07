"""YouTube provider: YouTube Data API (if a key is set) or yt-dlp for search, yt-dlp for download."""

from __future__ import annotations

import asyncio
import html
import logging
from pathlib import Path
from typing import Any

import httpx

from .base import BaseProvider, ExternalTrack, ProviderError, find_downloaded_file
from .registry import register_provider

logger = logging.getLogger(__name__)

YOUTUBE_API_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
WATCH_URL = "https://www.youtube.com/watch?v={id}"
OUTPUT_TEMPLATE = "%(artist,uploader)s - %(title)s [%(id)s].%(ext)s"


def split_artist_title(title: str, channel: str | None) -> tuple[str, str]:
    """Best-effort split of ``"Artist - Song"`` style video titles."""
    channel = (channel or "").strip()
    if channel.endswith(" - Topic"):
        channel = channel[: -len(" - Topic")]
    if " - " in title:
        artist, song = title.split(" - ", 1)
        if artist.strip() and song.strip():
            return artist.strip(), song.strip()
    return channel or "Unknown Artist", title


@register_provider
class YouTubeProvider(BaseProvider):
    name = "youtube"
    prefix = "yt"
    display_name = "YouTube"

    def __init__(self, settings: Any) -> None:
        super().__init__(settings)
        self.api_key = settings.get("YOUTUBE_API_KEY")

    # -- search -------------------------------------------------------------
    async def search(self, query: str, limit: int) -> list[ExternalTrack]:
        if self.api_key:
            return await self._search_api(query, limit)
        return await asyncio.to_thread(self._search_ytdlp, query, limit)

    async def _search_api(self, query: str, limit: int) -> list[ExternalTrack]:
        params = {
            "part": "snippet",
            "type": "video",
            "videoCategoryId": "10",  # Music
            "maxResults": str(min(max(limit, 1), 50)),
            "q": query,
            "key": self.api_key,
        }
        async with httpx.AsyncClient(timeout=self.settings.provider_timeout) as client:
            response = await client.get(YOUTUBE_API_SEARCH_URL, params=params)
        if response.status_code != 200:
            raise ProviderError(f"YouTube API returned HTTP {response.status_code}")
        tracks = []
        for item in response.json().get("items", []):
            video_id = (item.get("id") or {}).get("videoId")
            snippet = item.get("snippet") or {}
            if not video_id:
                continue
            artist, title = split_artist_title(
                html.unescape(snippet.get("title", "")), html.unescape(snippet.get("channelTitle", ""))
            )
            thumbs = snippet.get("thumbnails") or {}
            thumb = next(
                (thumbs[k]["url"] for k in ("high", "medium", "default") if k in thumbs), None
            )
            tracks.append(
                ExternalTrack(
                    provider=self.name,
                    item_id=video_id,
                    title=title,
                    artist=artist,
                    album=html.unescape(snippet.get("channelTitle", "")) or "YouTube",
                    thumb=thumb,
                    url=WATCH_URL.format(id=video_id),
                )
            )
        return tracks

    def _entry_to_track(self, entry: dict[str, Any]) -> ExternalTrack | None:
        video_id = entry.get("id")
        if not video_id:
            return None
        channel = entry.get("channel") or entry.get("uploader")
        if entry.get("artist") and entry.get("track"):
            artist, title = entry["artist"], entry["track"]
        else:
            artist, title = split_artist_title(entry.get("title") or video_id, channel)
        thumb = entry.get("thumbnail")
        if not thumb and entry.get("thumbnails"):
            thumb = entry["thumbnails"][-1].get("url")
        duration = entry.get("duration")
        return ExternalTrack(
            provider=self.name,
            item_id=video_id,
            title=title,
            artist=artist,
            album=entry.get("album") or channel or "YouTube",
            duration_ms=int(duration * 1000) if duration else None,
            thumb=thumb,
            url=WATCH_URL.format(id=video_id),
        )

    def _search_ytdlp(self, query: str, limit: int) -> list[ExternalTrack]:
        import yt_dlp

        opts = {"quiet": True, "no_warnings": True, "extract_flat": "in_playlist", "skip_download": True}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"ytsearch{max(limit, 1)}:{query}", download=False)
        entries = (info or {}).get("entries") or []
        return [t for t in (self._entry_to_track(e) for e in entries if e) if t]

    # -- metadata -----------------------------------------------------------
    async def fetch_metadata(self, item_id: str) -> ExternalTrack | None:
        return await asyncio.to_thread(self._fetch_metadata_sync, item_id)

    def _fetch_metadata_sync(self, item_id: str) -> ExternalTrack | None:
        import yt_dlp

        opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True}
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(WATCH_URL.format(id=item_id), download=False)
        except yt_dlp.utils.DownloadError:
            return None
        return self._entry_to_track(info) if info else None

    # -- download -----------------------------------------------------------
    async def download(self, item_id: str, output_dir: Path) -> Path:
        output_dir.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(self._download_sync, item_id, output_dir)
        path = find_downloaded_file(output_dir, item_id)
        if path is None:
            raise ProviderError(f"yt-dlp finished but no file found for {item_id}")
        return path

    def _download_sync(self, item_id: str, output_dir: Path) -> None:
        import yt_dlp

        opts = {
            "format": "bestaudio/best",
            "outtmpl": str(output_dir / OUTPUT_TEMPLATE),
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "postprocessors": [
                {"key": "FFmpegExtractAudio", "preferredcodec": self.settings.audio_format},
                {"key": "FFmpegMetadata", "add_metadata": True},
            ],
        }
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([WATCH_URL.format(id=item_id)])
        except yt_dlp.utils.DownloadError as exc:
            raise ProviderError(f"yt-dlp failed for {item_id}: {exc}") from exc
