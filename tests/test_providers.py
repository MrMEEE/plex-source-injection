import json
import os
from pathlib import Path

import pytest

from config import Settings
from providers import (
    BaseProvider,
    ProviderRegistry,
    discover_providers,
    is_external_id,
    make_external_id,
    parse_external_id,
    register_provider,
)
from providers.registry import unregister_provider
from providers.spotify import SpotifyProvider
from providers.youtube import YouTubeProvider, split_artist_title
from tests.conftest import FakeProvider, make_provider_class


@pytest.mark.parametrize(
    "prefix,item_id",
    [("yt", "dQw4w9WgXcQ"), ("yt", "a_b-c_d"), ("sp", "4uLU6hMCjMI75M1A2tKUQC"), ("sc", "123")],
)
def test_external_id_roundtrip(prefix, item_id):
    external_id = make_external_id(prefix, item_id)
    assert external_id == f"ext_{prefix}_{item_id}"
    assert is_external_id(external_id)
    assert parse_external_id(external_id) == (prefix, item_id)


@pytest.mark.parametrize(
    "value", ["12345", "ext_", "ext_yt_", "ext_YT_abc", "ext_yt_abc/../x", "ext_yt_a b", "xext_yt_abc"]
)
def test_parse_external_id_rejects_invalid(value):
    assert not is_external_id(value)
    with pytest.raises(ValueError):
        parse_external_id(value)


@pytest.mark.parametrize("prefix,item_id", [("Y_T", "abc"), ("yt", "../etc"), ("yt", "")])
def test_make_external_id_rejects_invalid(prefix, item_id):
    with pytest.raises(ValueError):
        make_external_id(prefix, item_id)


def test_provider_external_id_uses_prefix(settings):
    assert FakeProvider(settings).external_id("abc") == "ext_fk_abc"


def test_builtin_providers_are_discovered():
    classes = discover_providers()
    assert classes["youtube"] is YouTubeProvider
    assert classes["spotify"] is SpotifyProvider
    assert YouTubeProvider.prefix == "yt"
    assert SpotifyProvider.prefix == "sp"


def test_registry_from_settings_skips_unconfigured_and_unknown():
    settings = Settings.from_env({"ENABLED_PROVIDERS": "youtube, spotify, nope"})
    registry = ProviderRegistry.from_settings(settings)
    assert [p.name for p in registry.providers] == ["youtube"]


def test_registry_from_settings_enables_spotify_with_credentials():
    settings = Settings.from_env(
        {"ENABLED_PROVIDERS": "spotify", "SPOTIFY_CLIENT_ID": "id", "SPOTIFY_CLIENT_SECRET": "secret"}
    )
    registry = ProviderRegistry.from_settings(settings)
    assert [p.name for p in registry.providers] == ["spotify"]


def test_registry_resolve(settings):
    registry = ProviderRegistry([FakeProvider(settings)])
    provider, item_id = registry.resolve("ext_fk_some_id")
    assert provider.name == "fake" and item_id == "some_id"
    with pytest.raises(KeyError):
        registry.resolve("ext_zz_abc")
    with pytest.raises(ValueError):
        registry.resolve("1234")


def test_registry_rejects_duplicate_prefix(settings):
    other = make_provider_class("other", "fk")
    with pytest.raises(ValueError):
        ProviderRegistry([FakeProvider(settings), other(settings)])


def test_register_new_provider_is_loaded_from_settings():
    cls = register_provider(make_provider_class("soundcloudtest", "sct", "SoundCloud"))
    try:
        registry = ProviderRegistry.from_settings(Settings.from_env({"ENABLED_PROVIDERS": "soundcloudtest"}))
        assert registry.get_by_prefix("sct").__class__ is cls
    finally:
        unregister_provider("soundcloudtest")


def test_register_provider_validates_interface():
    class Incomplete(BaseProvider):
        name = "incomplete"
        prefix = "inc"
        display_name = "Incomplete"

        async def search(self, query, limit):
            return []

    with pytest.raises(TypeError):
        register_provider(Incomplete)
    with pytest.raises(TypeError):
        register_provider(make_provider_class("badprefix", "bad_prefix"))
    with pytest.raises(ValueError):
        register_provider(make_provider_class("dupe", "yt"))


@pytest.mark.parametrize(
    "title,channel,expected",
    [
        ("Daft Punk - One More Time", "Daft Punk", ("Daft Punk", "One More Time")),
        ("One More Time", "Daft Punk - Topic", ("Daft Punk", "One More Time")),
        ("Just a title", None, ("Unknown Artist", "Just a title")),
    ],
)
def test_split_artist_title(title, channel, expected):
    assert split_artist_title(title, channel) == expected


def test_youtube_entry_to_track(settings):
    provider = YouTubeProvider(settings)
    track = provider._entry_to_track(
        {"id": "abc_123", "title": "Artist - Song", "channel": "Chan", "duration": 61.5,
         "thumbnails": [{"url": "small"}, {"url": "big"}]}
    )
    assert (track.item_id, track.artist, track.title, track.album) == ("abc_123", "Artist", "Song", "Chan")
    assert track.duration_ms == 61500 and track.thumb == "big"


def test_spotify_to_track():
    settings = Settings.from_env({"SPOTIFY_CLIENT_ID": "id", "SPOTIFY_CLIENT_SECRET": "secret"})
    provider = SpotifyProvider(settings)
    track = provider._to_track(
        {"id": "4uLU6hMCjMI75M1A2tKUQC", "name": "Song", "duration_ms": 1000,
         "artists": [{"name": "A"}, {"name": "B"}],
         "album": {"name": "Album", "images": [{"url": "img"}]}}
    )
    assert (track.artist, track.title, track.album, track.thumb) == ("A, B", "Song", "Album", "img")


@pytest.mark.parametrize("opt_in,expected", [(None, True), ("true", True), ("false", False)])
def test_spotdl_gets_credentials_through_private_config(tmp_path, monkeypatch, opt_in, expected):
    import asyncio
    import json
    import os

    import providers.spotify as spotify

    captured = {}

    class Process:
        returncode = 0

        async def communicate(self):
            (tmp_path / "A - B [4uLU6hMCjMI75M1A2tKUQC].mp3").write_bytes(b"x")
            return b"", None

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        home = kwargs["env"].get("HOME", "")
        config = os.path.join(home, ".config", "spotdl", "config.json")
        if os.path.isfile(config):
            captured["config"] = json.loads(open(config).read())
            captured["mode"] = os.stat(config).st_mode & 0o777
            captured["home"] = home
        return Process()

    monkeypatch.setenv("HOME", str(tmp_path / "real-home"))
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.setattr(spotify.shutil, "which", lambda name: "/usr/bin/spotdl")
    monkeypatch.setattr(spotify.asyncio, "create_subprocess_exec", fake_exec)
    values = {"SPOTIFY_CLIENT_ID": "id", "SPOTIFY_CLIENT_SECRET": "secret"}
    if opt_in is not None:
        values["SPOTDL_PROVIDER_CREDENTIALS"] = opt_in
    path = asyncio.run(SpotifyProvider(Settings.from_env(values)).download("4uLU6hMCjMI75M1A2tKUQC", tmp_path))
    assert path.name == "A - B [4uLU6hMCjMI75M1A2tKUQC].mp3"
    assert "secret" not in captured["args"]
    assert captured["args"][-1] == "--lyrics"
    assert "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC" in captured["args"]
    assert ("config" in captured) is expected
    if expected:
        assert captured["config"] == {
            "client_id": "id", "client_secret": "secret", "use_official_api": True, "load_config": True
        }
        assert captured["mode"] == 0o600
        assert not os.path.exists(captured["home"])


class FakeSpotifyClient:
    def __init__(self, fail=False):
        self.fail = fail

    def track(self, track_id):
        if self.fail:
            raise RuntimeError("api down")
        return {
            "id": track_id, "name": "B", "duration_ms": 61500, "disc_number": 1, "track_number": 3,
            "explicit": False, "popularity": 7, "external_ids": {"isrc": "DKXX1"},
            "external_urls": {"spotify": f"https://open.spotify.com/track/{track_id}"},
            "artists": [{"id": "art1", "name": "A"}, {"id": "art2", "name": "C"}],
            "album": {"id": "alb1", "name": "Album", "total_tracks": 9},
        }

    def album(self, album_id):
        return {
            "id": album_id, "name": "Album", "artists": [{"name": "A"}], "album_type": "single",
            "release_date": "2024-05-01", "total_tracks": 9, "label": "Label", "genres": [],
            "copyrights": [{"text": "(C) Label"}], "tracks": {"items": [{"disc_number": 1}, {"disc_number": 2}]},
            "images": [{"url": "small", "width": 64, "height": 64}, {"url": "big", "width": 640, "height": 640}],
        }

    def artist(self, artist_id):
        return {"genres": ["danish pop"]}


@pytest.mark.parametrize("fail", [False, True])
def test_spotdl_download_reuses_prescan_youtube_match(tmp_path, monkeypatch, fail):
    import asyncio
    import time

    import providers.spotify as spotify

    captured = {}
    out = tmp_path / "out"

    class Process:
        returncode = 0

        async def communicate(self):
            (out / "A - B [4uLU6hMCjMI75M1A2tKUQC].mp3").write_bytes(b"x")
            return b"", None

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        if args[2].endswith(".spotdl"):
            captured["songs"] = json.loads(Path(args[2]).read_text())
        return Process()

    monkeypatch.setattr(spotify.shutil, "which", lambda name: "/usr/bin/spotdl")
    monkeypatch.setattr(spotify.asyncio, "create_subprocess_exec", fake_exec)
    provider = SpotifyProvider(Settings.from_env({"SPOTIFY_CLIENT_ID": "id", "SPOTIFY_CLIENT_SECRET": "secret"}))
    provider._client = FakeSpotifyClient(fail=fail)
    provider._availability["4uLU6hMCjMI75M1A2tKUQC"] = (time.monotonic() - 1, "dQw4w9WgXcQ")
    asyncio.run(provider.download("4uLU6hMCjMI75M1A2tKUQC", out))
    if fail:
        assert captured["args"][2] == (
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ|https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC"
        )
        return
    assert not os.path.exists(captured["args"][2])
    [song] = captured["songs"]
    assert song["download_url"] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert song["artists"] == ["A", "C"] and song["genres"] == ["danish pop"]
    assert (song["year"], song["disc_count"], song["tracks_count"], song["duration"]) == (2024, 2, 9, 61)
    assert (song["cover_url"], song["publisher"], song["copyright_text"]) == ("big", "Label", "(C) Label")
    assert song["isrc"] == "DKXX1"
    # Every field spotdl checks before re-fetching from Spotify must be present.
    for key in ("genres", "disc_count", "tracks_count", "track_number", "album_id", "album_artist"):
        assert song[key] is not None


def test_spotdl_no_file_reports_downloader_output(tmp_path, monkeypatch):
    import asyncio
    import providers.spotify as spotify

    class Process:
        returncode = 0

        async def communicate(self):
            return b"Skipping song: no matching audio source found", None

    async def fake_exec(*args, **kwargs):
        return Process()

    monkeypatch.setattr(spotify.shutil, "which", lambda name: "/usr/bin/spotdl")
    monkeypatch.setattr(spotify.asyncio, "create_subprocess_exec", fake_exec)
    settings = Settings.from_env({"SPOTIFY_CLIENT_ID": "id", "SPOTIFY_CLIENT_SECRET": "secret"})
    with pytest.raises(spotify.ProviderError, match="Skipping song: no matching audio source found"):
        asyncio.run(SpotifyProvider(settings).download("4uLU6hMCjMI75M1A2tKUQC", tmp_path))


def test_spotify_prescan_filters_caches_and_preserves_order(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    settings = Settings.from_env({"SPOTIFY_CLIENT_ID": "id", "SPOTIFY_CLIENT_SECRET": "secret"})
    provider = SpotifyProvider(settings)
    ids = ["a" * 22, "b" * 22, "c" * 22]
    provider._client = SimpleNamespace(search=lambda **kwargs: {
        "tracks": {"items": [{"id": value, "name": value} for value in ids]}
    })
    checks = []

    async def available(track):
        checks.append(track.item_id)
        return None if track.item_id == ids[1] else "dQw4w9WgXcQ"

    monkeypatch.setattr(provider, "_source_available", available)

    async def run():
        for _ in range(2):
            assert [track.item_id for track in await provider.search("song", 10)] == [ids[0], ids[2]]
        assert checks == ids
        provider._availability[ids[1]] = (0, None)
        await provider.search("song", 10)
        assert checks == [*ids, ids[1]]
    asyncio.run(run())


def test_spotify_prescan_timeout_is_hidden_and_cancels_checks(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    settings = Settings.from_env({"SPOTIFY_CLIENT_ID": "id", "SPOTIFY_CLIENT_SECRET": "secret", "PROVIDER_TIMEOUT": "0.1"})
    provider = SpotifyProvider(settings)
    provider._client = SimpleNamespace(search=lambda **kwargs: {"tracks": {"items": [{"id": "a" * 22}]}})
    cancelled = []

    async def available(track):
        try:
            await asyncio.sleep(5)
        finally:
            cancelled.append(track.item_id)

    monkeypatch.setattr(provider, "_source_available", available)
    assert asyncio.run(provider.search("song", 10)) == []
    assert cancelled == ["a" * 22]
    assert not provider._availability


def test_spotify_prescan_runs_three_checks_in_parallel(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    settings = Settings.from_env({"SPOTIFY_CLIENT_ID": "id", "SPOTIFY_CLIENT_SECRET": "secret"})
    provider = SpotifyProvider(settings)
    ids = [str(number) * 22 for number in range(7)]
    provider._client = SimpleNamespace(search=lambda **kwargs: {
        "tracks": {"items": [{"id": value} for value in ids]}
    })
    active = 0
    peak = 0

    async def available(track):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.02)
        active -= 1
        return "dQw4w9WgXcQ"

    monkeypatch.setattr(provider, "_source_available", available)
    assert [track.item_id for track in asyncio.run(provider.search("song", 10))] == ids
    assert peak == 3
    assert active == 0


def test_spotify_prescan_can_be_disabled_and_errors_are_not_cached(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    import providers.spotify as spotify
    provider = SpotifyProvider(Settings.from_env({
        "SPOTIFY_CLIENT_ID": "id", "SPOTIFY_CLIENT_SECRET": "secret",
        "SPOTIFY_CHECK_AVAILABILITY": "false",
    }))
    provider._client = SimpleNamespace(search=lambda **kwargs: {"tracks": {"items": [{"id": "a" * 22}]}})
    calls = []

    async def failed(track):
        calls.append(track.item_id)
        raise spotify.ProviderError("source unavailable")

    monkeypatch.setattr(provider, "_source_available", failed)

    async def run():
        assert len(await provider.search("song", 10)) == 1
        assert not calls
        provider.check_availability = True
        for _ in range(2):
            assert await provider.search("song", 10) == []
        assert calls == ["a" * 22] * 2
        assert not provider._availability
    asyncio.run(run())


def _checked_track():
    from providers.base import ExternalTrack
    return ExternalTrack(provider="spotify", item_id="a" * 22, title="Klip Klap", artist="APHACA")


@pytest.mark.parametrize("output,expected", [
    (b"dQw4w9WgXcQ\n", "dQw4w9WgXcQ"),
    (b"NA\n", None),
    (b"", None),
])
def test_spotify_source_check_searches_youtube_with_cli(monkeypatch, output, expected):
    import asyncio
    import providers.spotify as spotify
    calls = []

    class Process:
        returncode = 0
        async def communicate(self):
            return output, b""

    async def execute(*args, **kwargs):
        calls.append(args)
        return Process()

    monkeypatch.setattr(spotify, "resolve_tool", lambda settings, name: f"/opt/{name}")
    monkeypatch.setattr(spotify.asyncio, "create_subprocess_exec", execute)
    provider = SpotifyProvider(Settings.from_env({
        "SPOTIFY_CLIENT_ID": "id", "SPOTIFY_CLIENT_SECRET": "secret", "YTDLP_MODE": "managed",
    }))
    assert asyncio.run(provider._source_available(_checked_track())) == expected
    assert calls[0][0] == "/opt/yt-dlp"
    assert calls[0][-1] == "ytsearch1:APHACA - Klip Klap"
    assert "--flat-playlist" in calls[0]


def test_spotify_source_check_uses_bundled_module(monkeypatch):
    import asyncio
    import sys
    from types import SimpleNamespace
    queries = []

    class YoutubeDL:
        def __init__(self, opts):
            assert opts["extract_flat"] == "in_playlist"
        def __enter__(self):
            return self
        def __exit__(self, *exc):
            return False
        def extract_info(self, query, download):
            queries.append(query)
            return {"entries": [{"id": "dQw4w9WgXcQ"}]}

    monkeypatch.setitem(sys.modules, "yt_dlp", SimpleNamespace(YoutubeDL=YoutubeDL))
    provider = SpotifyProvider(Settings.from_env({"SPOTIFY_CLIENT_ID": "id", "SPOTIFY_CLIENT_SECRET": "secret"}))
    assert asyncio.run(provider._source_available(_checked_track())) == "dQw4w9WgXcQ"
    assert queries == ["ytsearch1:APHACA - Klip Klap"]


def test_spotify_source_check_cancellation_reaps_process(monkeypatch):
    import asyncio
    import providers.spotify as spotify
    actions = []

    class Process:
        returncode = None
        async def communicate(self):
            raise asyncio.CancelledError
        def kill(self):
            actions.append("kill")
        async def wait(self):
            actions.append("wait")

    async def execute(*args, **kwargs):
        return Process()

    monkeypatch.setattr(spotify, "resolve_tool", lambda settings, name: f"/opt/{name}")
    monkeypatch.setattr(spotify.asyncio, "create_subprocess_exec", execute)
    provider = SpotifyProvider(Settings.from_env({
        "SPOTIFY_CLIENT_ID": "id", "SPOTIFY_CLIENT_SECRET": "secret", "YTDLP_MODE": "managed",
    }))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(provider._source_available(_checked_track()))
    assert actions == ["kill", "wait"]


def test_youtube_external_cli_search_metadata_download(tmp_path, monkeypatch):
    import asyncio
    import json

    calls = []
    track_id = "abc123"

    class Process:
        returncode = 0

        def __init__(self, args):
            self.args = args

        async def communicate(self):
            if any(argument.startswith("ytsearch") for argument in self.args):
                return json.dumps({"entries": [{"id": track_id, "title": "Artist - Song"}]}).encode(), b""
            if "--skip-download" in self.args:
                return json.dumps({"id": track_id, "title": "Artist - Song"}).encode(), b""
            (tmp_path / f"Artist - Song [{track_id}].mp3").write_bytes(b"audio")
            return b"", b""

    async def execute(*args, **kwargs):
        calls.append(args)
        return Process(args)

    monkeypatch.setattr("dependencies.shutil.which", lambda binary: "/provided/yt-dlp")
    monkeypatch.setattr("providers.youtube.asyncio.create_subprocess_exec", execute)
    provider = YouTubeProvider(Settings.from_env({"YTDLP_MODE": "external", "YTDLP_BINARY": "my-yt-dlp"}))
    assert asyncio.run(provider.search("song", 5))[0].item_id == track_id
    assert asyncio.run(provider.fetch_metadata(track_id)).title == "Song"
    assert asyncio.run(provider.download(track_id, tmp_path)).is_file()
    assert all(args[:2] == ("/provided/yt-dlp", "--ignore-config") for args in calls)
    assert "--extract-audio" in calls[-1]


def test_managed_spotdl_and_ffmpeg_paths_are_used(tmp_path, monkeypatch):
    import asyncio
    import json

    root = tmp_path / "tools"
    for name in ("spotdl", "ffmpeg"):
        folder = root / name / "v1"
        folder.mkdir(parents=True)
        (folder / name).write_text("executable")
        (root / name / "current.json").write_text(json.dumps({"binary": str(folder / name), "version": "v1"}))
    settings = Settings.from_env({
        "SPOTIFY_CLIENT_ID": "id", "SPOTIFY_CLIENT_SECRET": "secret",
        "DEPENDENCY_DIR": str(root), "SPOTDL_MODE": "managed", "FFMPEG_MODE": "managed",
    })
    calls = []

    class Process:
        returncode = 0

        async def communicate(self):
            (tmp_path / "Artist - Song [4uLU6hMCjMI75M1A2tKUQC].mp3").write_bytes(b"audio")
            return b"", None

    async def execute(*args, **kwargs):
        calls.append(args)
        return Process()

    monkeypatch.setattr("providers.spotify.asyncio.create_subprocess_exec", execute)
    provider = SpotifyProvider(settings)
    asyncio.run(provider.download("4uLU6hMCjMI75M1A2tKUQC", tmp_path))
    assert calls[0][0] == str(root / "spotdl" / "v1" / "spotdl")
    assert calls[0][-3:] == ("--ffmpeg", str(root / "ffmpeg" / "v1" / "ffmpeg"), "--lyrics")
