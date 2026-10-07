import asyncio

import httpx
import pytest

from ingest import IngestError, Ingestor, ItemNotFoundError
from providers import ProviderRegistry
from tests.conftest import FakeProvider, track


class FakeLookup:
    """Simulates Plex: a track becomes visible only after a scan was requested."""

    def __init__(self, rating_key="4242", indexed=False):
        self.rating_key = rating_key
        self.indexed = indexed
        self.calls = []

    def __call__(self, item_id, title):
        self.calls.append((item_id, title))
        return self.rating_key if self.indexed else None


def make_ingestor(settings, provider, lookup, scans):
    def handler(request: httpx.Request) -> httpx.Response:
        scans.append(request)
        lookup.indexed = True
        return httpx.Response(200)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return Ingestor(settings, ProviderRegistry([provider]), http_client=client, plex_lookup=lookup)


def test_download_and_register_downloads_scans_and_polls(settings):
    provider = FakeProvider(settings, tracks=[track("abc_1", title="Song")])
    lookup, scans = FakeLookup(), []
    ingestor = make_ingestor(settings, provider, lookup, scans)

    async def run():
        return await asyncio.gather(*(ingestor.download_and_register("ext_fk_abc_1") for _ in range(3)))

    assert asyncio.run(run()) == ["4242"] * 3
    assert provider.downloads == ["abc_1"]
    assert (settings.download_dir / "Artist - Title [abc_1].mp3").exists()
    assert len(scans) == 1
    scan = scans[0]
    assert scan.url.path == "/library/sections/3/refresh"
    assert scan.url.params["path"] == "/music/Downloads"
    assert scan.headers["X-Plex-Token"] == "server-token"
    assert lookup.calls[-1] == ("abc_1", "Song")

    # Cached afterwards: no new download or scan.
    assert asyncio.run(ingestor.download_and_register("ext_fk_abc_1")) == "4242"
    assert provider.downloads == ["abc_1"] and len(scans) == 1


def test_already_indexed_item_is_not_downloaded(settings):
    provider = FakeProvider(settings, tracks=[track("abc")])
    lookup, scans = FakeLookup(indexed=True), []
    ingestor = make_ingestor(settings, provider, lookup, scans)
    assert asyncio.run(ingestor.download_and_register("ext_fk_abc")) == "4242"
    assert provider.downloads == [] and scans == []


def test_unknown_item_raises_not_found(settings):
    ingestor = make_ingestor(settings, FakeProvider(settings), FakeLookup(), [])
    with pytest.raises(ItemNotFoundError):
        asyncio.run(ingestor.download_and_register("ext_fk_missing"))


def test_unknown_provider_prefix_raises_key_error(settings):
    ingestor = make_ingestor(settings, FakeProvider(settings), FakeLookup(), [])
    with pytest.raises(KeyError):
        asyncio.run(ingestor.download_and_register("ext_zz_abc"))


def test_download_failure_raises_ingest_error(settings):
    provider = FakeProvider(settings, tracks=[track("abc")], error=RuntimeError("down"))
    ingestor = make_ingestor(settings, provider, FakeLookup(), [])
    with pytest.raises(IngestError):
        asyncio.run(ingestor.download_and_register("ext_fk_abc"))


def test_poll_timeout_raises_ingest_error(settings):
    provider = FakeProvider(settings, tracks=[track("abc")])
    lookup = FakeLookup()

    def handler(request):
        return httpx.Response(200)  # scan accepted, but Plex never indexes the file

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    ingestor = Ingestor(settings, ProviderRegistry([provider]), http_client=client, plex_lookup=lookup)
    with pytest.raises(IngestError):
        asyncio.run(ingestor.download_and_register("ext_fk_abc"))


def test_scan_http_error_raises_ingest_error(settings):
    provider = FakeProvider(settings, tracks=[track("abc")])
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(401)))
    ingestor = Ingestor(settings, ProviderRegistry([provider]), http_client=client, plex_lookup=FakeLookup())
    with pytest.raises(IngestError):
        asyncio.run(ingestor.download_and_register("ext_fk_abc"))


def test_plex_api_lookup_matches_filename_marker(settings):
    from types import SimpleNamespace

    from ingest import PlexApiLookup

    def make_track(key, file):
        return SimpleNamespace(ratingKey=key, media=[SimpleNamespace(parts=[SimpleNamespace(file=file)])])

    class Section:
        type = "artist"

        def __init__(self):
            self.calls = []

        def search(self, **kwargs):
            self.calls.append(kwargs)
            if kwargs.get("title"):
                return [make_track(7, "/music/Downloads/A - Song [abc].mp3")]
            return [make_track(1, "/music/Downloads/A - Song [abcd].mp3")]

    lookup = PlexApiLookup(settings)
    lookup._section = section = Section()
    assert lookup("abc", None) is None
    assert lookup("abc", "Song") == "7"
    assert lookup("abcd", None) == "1"
    assert section.calls[0]["sort"] == "addedAt:desc" and section.calls[0]["libtype"] == "track"


def test_movie_library_rejected_before_track_search_or_download(settings):
    from types import SimpleNamespace
    from ingest import PlexApiLookup

    calls = []
    lookup = PlexApiLookup(settings)
    lookup._section = SimpleNamespace(type="movie", title="Movies", search=lambda **kwargs: calls.append(kwargs))
    provider = FakeProvider(settings, tracks=[track("abc")])
    ingestor = Ingestor(settings, ProviderRegistry([provider]), plex_lookup=lookup)
    with pytest.raises(IngestError, match="MUSIC_SECTION_ID=3.*Movies.*movie.*Music"):
        asyncio.run(ingestor.download_and_register("ext_fk_abc"))
    assert calls == []
    assert provider.downloads == []
    assert not settings.download_dir.exists()


def test_title_from_filename():
    from ingest import title_from_filename

    assert title_from_filename("Artist - Some - Song [a_b].mp3", "a_b") == "Some - Song"
    assert title_from_filename("Song [x].flac", "x") == "Song"


def test_poll_uses_filename_title_when_metadata_unavailable(settings):
    provider = FakeProvider(settings, tracks=[track("abc")])

    async def broken_metadata(item_id):
        raise RuntimeError("metadata down")

    provider.fetch_metadata = broken_metadata
    lookup, scans = FakeLookup(), []
    ingestor = make_ingestor(settings, provider, lookup, scans)
    assert asyncio.run(ingestor.download_and_register("ext_fk_abc")) == "4242"
    assert lookup.calls[0] == ("abc", None)
    assert lookup.calls[-1] == ("abc", "Title")
