import asyncio
from types import SimpleNamespace

import pytest
from plexapi.exceptions import NotFound, Unauthorized
from requests import ConnectionError

from ingest import IngestConfigurationError, Ingestor, PlexApiLookup
from plex_libraries import PlexLibraryError, music_libraries
from providers import ProviderRegistry
from tests.test_web_admin import admin


def test_discovery_returns_only_music_libraries(settings, monkeypatch):
    sections = [
        SimpleNamespace(key=1, title="Movies", type="movie"),
        SimpleNamespace(key=2, title="TV Shows", type="show"),
        SimpleNamespace(key=3, title="Music", type="artist"),
        SimpleNamespace(key=4, title="Audiobooks", type="artist"),
    ]
    def server(url, token, timeout, session):
        assert url == settings.plex_url and token == settings.plex_token
        assert timeout == 10
        return SimpleNamespace(library=SimpleNamespace(sections=lambda: sections))
    monkeypatch.setattr("plex_libraries.PlexServer", server)
    assert music_libraries(settings) == [{"id": "3", "title": "Music"}, {"id": "4", "title": "Audiobooks"}]


@pytest.mark.parametrize("error,message", [
    (Unauthorized("bad-token"), "Plex rejected the saved token"),
    (ConnectionError("server-token"), "Could not load Plex libraries"),
])
def test_discovery_connection_errors_are_explicit_and_redacted(settings, monkeypatch, error, message):
    def server(*args, **kwargs):
        raise error
    monkeypatch.setattr("plex_libraries.PlexServer", server)
    with pytest.raises(PlexLibraryError, match=message) as caught:
        music_libraries(settings)
    assert settings.plex_token not in str(caught.value)


def test_music_discovery_requires_saved_token():
    from config import Settings
    with pytest.raises(PlexLibraryError, match="Save your Plex server URL and token"):
        music_libraries(Settings())


def test_missing_section_has_actionable_error(settings, monkeypatch):
    def section_by_id(section_id):
        raise NotFound("missing")
    monkeypatch.setattr("plexapi.server.PlexServer", lambda *args, **kwargs:
        SimpleNamespace(library=SimpleNamespace(sectionByID=section_by_id)))
    with pytest.raises(IngestConfigurationError, match="MUSIC_SECTION_ID=3 was not found"):
        PlexApiLookup(settings)("abc", None)


def test_polling_does_not_retry_wrong_library(settings):
    calls = []
    def lookup(item_id, title):
        calls.append(item_id)
        raise IngestConfigurationError("Wrong library")
    ingestor = Ingestor(settings, ProviderRegistry(), plex_lookup=lookup)
    with pytest.raises(IngestConfigurationError, match="Wrong library"):
        asyncio.run(ingestor.wait_for_rating_key("abc", None))
    assert calls == ["abc"]


def test_music_library_api_authenticated_and_does_not_expose_credentials(admin, monkeypatch):
    client, app, _ = admin
    def libraries(settings):
        assert settings is app.state.settings
        return [{"id": "3", "title": "My Music"}]
    monkeypatch.setattr("web_admin.music_libraries", libraries)
    response = client.get("/admin/api/plex/music-libraries")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"libraries": [{"id": "3", "title": "My Music"}]}
    assert "private-token" not in response.text
    client.cookies.clear()
    assert client.get("/admin/api/plex/music-libraries").status_code == 401


def test_music_library_api_failure_does_not_change_settings(admin, monkeypatch):
    client, app, store = admin
    before = store.values()
    runtime = app.state.runtime
    def fail(settings):
        raise PlexLibraryError("Plex rejected the saved token")
    monkeypatch.setattr("web_admin.music_libraries", fail)
    response = client.get("/admin/api/plex/music-libraries")
    assert response.status_code == 502
    assert response.json()["detail"] == "Plex rejected the saved token"
    assert store.values() == before
    assert app.state.runtime is runtime


def test_no_music_libraries_is_not_a_connection_error(admin, monkeypatch):
    client, _, _ = admin
    monkeypatch.setattr("web_admin.music_libraries", lambda settings: [])
    assert client.get("/admin/api/plex/music-libraries").json() == {"libraries": []}
