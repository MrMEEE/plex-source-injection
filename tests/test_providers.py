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


@pytest.mark.parametrize("opt_in,expected", [("", False), ("true", True)])
def test_spotdl_credentials_only_passed_when_opted_in(tmp_path, monkeypatch, opt_in, expected):
    import asyncio

    import providers.spotify as spotify

    captured = {}

    class Process:
        returncode = 0

        async def communicate(self):
            (tmp_path / "A - B [4uLU6hMCjMI75M1A2tKUQC].mp3").write_bytes(b"x")
            return b"", None

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        return Process()

    monkeypatch.setattr(spotify.shutil, "which", lambda name: "/usr/bin/spotdl")
    monkeypatch.setattr(spotify.asyncio, "create_subprocess_exec", fake_exec)
    settings = Settings.from_env(
        {"SPOTIFY_CLIENT_ID": "id", "SPOTIFY_CLIENT_SECRET": "secret", "SPOTDL_PASS_CREDENTIALS": opt_in}
    )
    path = asyncio.run(SpotifyProvider(settings).download("4uLU6hMCjMI75M1A2tKUQC", tmp_path))
    assert path.name == "A - B [4uLU6hMCjMI75M1A2tKUQC].mp3"
    assert ("secret" in captured["args"]) is expected
    assert "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC" in captured["args"]
