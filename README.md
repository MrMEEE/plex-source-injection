# plex-source-injection
On-Demand Search &amp; Ingest Proxy for Plex

A FastAPI reverse proxy that sits between Plex/Plexamp clients and your Plex Media Server.
It injects search results from external sources (YouTube, Spotify, …) into Plex search
responses and downloads + indexes an external track on demand when a client opens it.

```
Plexamp ──► proxy (:8080) ──► Plex Media Server (:32400)
              │
              ├─ /hubs/search, /library/search  → local results + external providers (concurrently)
              ├─ /library/metadata/ext_<p>_<id> → download → Plex partial scan → real track metadata
              └─ everything else                → streamed through unchanged
```

## How it works

1. **Search interception** – `GET /hubs/search` and `GET /library/search` are forwarded to
   Plex while every enabled provider is queried concurrently (`asyncio.gather`). External
   hits are appended to the JSON response as Plex `track` items with synthetic ratingKeys
   such as `ext_yt_<VIDEO_ID>` or `ext_sp_<TRACK_ID>`. A provider that errors or exceeds
   `PROVIDER_TIMEOUT` is skipped; local and remaining provider results are still returned.
   Results are only injected into successful (2xx) JSON responses, so unauthenticated
   clients never see them. XML responses are passed through unchanged.
2. **Ingestion** – when a client requests `/library/metadata/ext_<prefix>_<id>` (or creates
   a play queue whose `uri` references one), the proxy verifies the client's token against
   Plex, dispatches the download to the owning provider (`yt-dlp` / `spotdl`), writes it to
   `DOWNLOAD_DIR` as `Artist - Title [<id>].<ext>`, triggers
   `GET /library/sections/<MUSIC_SECTION_ID>/refresh?path=<PLEX_DOWNLOAD_DIR>`, polls Plex
   (via `python-plexapi`) until the file has a real ratingKey, and then returns Plex's own
   metadata for that ratingKey so the client plays the file natively. Concurrent requests
   for the same item share one download.
3. **Pass-through** – all other requests (including media streams and `Range` requests) are
   streamed to Plex with their `X-Plex-*` headers and query strings untouched.
4. **Cleanup** – every `CLEANUP_INTERVAL_HOURS` (default 24) files in `DOWNLOAD_DIR` older
   than `RETENTION_DAYS` are deleted and the folder is rescanned. `RETENTION_DAYS=0`
   disables cleanup.

## Installation

Requires Python 3.11+ and `ffmpeg` on `PATH`.

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
# spotdl pins old fastapi/uvicorn versions, so install it in an isolated environment:
pipx install spotdl
cp .env.example .env   # then edit it
python main.py         # or: uvicorn main:create_app --factory --port 8080
```

Point your Plex clients at `http://<proxy-host>:<PROXY_PORT>` instead of the Plex server.

## Configuration

| Variable | Default | Description |
| --- | --- | --- |
| `PLEX_URL` | `http://127.0.0.1:32400` | Upstream Plex Media Server |
| `PLEX_TOKEN` | – | Token used for scans and polling |
| `MUSIC_SECTION_ID` | `1` | Music library section that contains `DOWNLOAD_DIR` |
| `DOWNLOAD_DIR` | `/music/Downloads` | Where the proxy writes downloads |
| `PLEX_DOWNLOAD_DIR` | `DOWNLOAD_DIR` | Same folder as seen by Plex (if paths differ, e.g. containers) |
| `RETENTION_DAYS` | `30` | Delete downloads older than this (`0` disables) |
| `PROXY_PORT` | `8080` | Listening port |
| `ENABLED_PROVIDERS` | `youtube,spotify` | Comma-separated provider names |
| `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET` | – | Required for the Spotify provider |
| `YOUTUBE_API_KEY` | – | Optional; uses the YouTube Data API for search instead of `yt-dlp` |
| `AUDIO_FORMAT` | `mp3` | Output format (`mp3`, `flac`, `opus`, …) |
| `SEARCH_LIMIT` | `10` | Max results per provider |
| `PROVIDER_TIMEOUT` | `8` | Seconds before a provider search is abandoned |
| `DOWNLOAD_TIMEOUT` | `300` | Seconds before a download is abandoned |
| `SCAN_TIMEOUT` / `SCAN_POLL_INTERVAL` | `120` / `2` | Polling for the newly scanned track |
| `CLEANUP_INTERVAL_HOURS` | `24` | Cleanup interval |
| `SPOTDL_BINARY` | `spotdl` | spotdl executable name/path |

## Adding a provider

Create a module in `providers/` – no changes to the proxy, search or ingest code are needed:

```python
# providers/soundcloud.py
from pathlib import Path

from .base import BaseProvider, ExternalTrack, ProviderConfigurationError
from .registry import register_provider


@register_provider
class SoundCloudProvider(BaseProvider):
    name = "soundcloud"      # used in ENABLED_PROVIDERS
    prefix = "sc"            # ratingKeys become ext_sc_<id> (lowercase alphanumerics)
    display_name = "SoundCloud"

    def __init__(self, settings):
        super().__init__(settings)
        self.token = settings.get("SOUNDCLOUD_TOKEN")  # any env var is available
        if not self.token:
            raise ProviderConfigurationError("SOUNDCLOUD_TOKEN not set")  # provider is skipped

    async def search(self, query: str, limit: int) -> list[ExternalTrack]: ...
    async def fetch_metadata(self, item_id: str) -> ExternalTrack | None: ...
    async def download(self, item_id: str, output_dir: Path) -> Path:
        ...  # must write "<anything> [<item_id>].<ext>" into output_dir and return the path
```

Then add `soundcloud` to `ENABLED_PROVIDERS`. Item IDs must match `[A-Za-z0-9_-]+`.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

## Limitations

* Only JSON search responses are augmented (Plexamp requests JSON); XML is passed through.
* WebSocket endpoints (e.g. `/:/websockets/notifications`) are not proxied.
* spotdl receives the Spotify credentials as command-line arguments.
