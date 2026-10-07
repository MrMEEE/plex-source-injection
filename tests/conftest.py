import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import Settings  # noqa: E402
from providers import BaseProvider, ExternalTrack, ProviderError  # noqa: E402


class FakeProvider(BaseProvider):
    name = "fake"
    prefix = "fk"
    display_name = "Fake"

    def __init__(self, settings, tracks=None, delay=0.0, error=None):
        super().__init__(settings)
        self.tracks = tracks if tracks is not None else []
        self.delay = delay
        self.error = error
        self.searches = []
        self.downloads = []

    async def search(self, query, limit):
        self.searches.append((query, limit))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return list(self.tracks)

    async def fetch_metadata(self, item_id):
        for track in self.tracks:
            if track.item_id == item_id:
                return track
        return None

    async def download(self, item_id, output_dir):
        self.downloads.append(item_id)
        if self.error:
            raise ProviderError("boom")
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / f"Artist - Title [{item_id}].mp3"
        path.write_bytes(b"audio")
        return path


def make_provider_class(name, prefix, display_name=None):
    return type(
        f"{name.title()}Provider",
        (FakeProvider,),
        {"name": name, "prefix": prefix, "display_name": display_name or name.title()},
    )


def track(item_id, title="Song", artist="Artist", provider="fake", **kwargs):
    return ExternalTrack(provider=provider, item_id=item_id, title=title, artist=artist, **kwargs)


@pytest.fixture
def settings(tmp_path):
    return Settings.from_env(
        {
            "PLEX_URL": "http://plex.test:32400",
            "PLEX_TOKEN": "server-token",
            "MUSIC_SECTION_ID": "3",
            "DOWNLOAD_DIR": str(tmp_path / "Downloads"),
            "PLEX_DOWNLOAD_DIR": "/music/Downloads",
            "ENABLED_PROVIDERS": "youtube",
            "PROVIDER_TIMEOUT": "0.2",
            "SCAN_TIMEOUT": "1",
            "SCAN_POLL_INTERVAL": "0.01",
        }
    )
