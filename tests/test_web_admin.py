import asyncio
import json
import threading
import re
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi.testclient import TestClient

from config_store import ConfigStore
from tests.admin_client import create_app
from providers import ProviderRegistry
from providers.base import ProviderSetting
from providers.registry import register_provider, unregister_provider
from tests.conftest import FakeProvider, make_provider_class, track
from tests.test_main import _AsyncBody, streaming

AUTH = ("admin", "test-password-123")

def sign_in(client, password=AUTH[1]):
    page = client.get("/admin/login", follow_redirects=False)
    token = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    response = client.post("/admin/login", data={"username": "admin", "password": password, "csrf": token}, follow_redirects=False)
    if response.status_code == 303:
        session = client.get("/admin/api/session").json()
        client.headers["X-CSRF-Token"] = session["csrf"]
    return response


@pytest.fixture
def admin(tmp_path):
    store = ConfigStore(tmp_path / "config.sqlite3")
    store.initialize({"DOWNLOAD_DIR": str(tmp_path), "PLEX_TOKEN": "private-token"})
    store.set_password(AUTH[1])
    app = create_app(store=store, enable_cleanup=False, upstream_transport=streaming(
        lambda request: httpx.Response(200, json={"host": request.url.host})
    ))
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        sign_in(client)
        yield client, app, store


def config(client):
    if "plex_admin_session" not in client.cookies:
        sign_in(client)
    response = client.get("/admin/api/config")
    assert response.status_code == 200
    return response.json()


def update(client, values, **kwargs):
    return client.put("/admin/api/config", json={
        "revision": config(client)["revision"], "values": values, **kwargs,
    })


def test_admin_authentication_and_assets(admin):
    client, _, _ = admin
    client.cookies.clear()
    for path in ("/admin", "/admin/"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/admin/login"
        assert "www-authenticate" not in response.headers
    for path in ("/admin/api/config", "/admin/assets/admin.js"):
        assert client.get(path).status_code == 401
    assert client.get("/admin/login").status_code == 200
    assert client.get("/admin/assets/admin.css").status_code == 200
    assert sign_in(client, "wrong").status_code == 401
    assert sign_in(client).status_code == 303
    assert client.get("/admin/").status_code == 200
    assert client.get("/admin/assets/admin.js").status_code == 200
    assert client.get("/admin/assets/admin.css").status_code == 200
    assert client.get("/admin/not-a-plex-route").status_code == 404
    assert client.options("/admin/api/config").status_code == 404


def test_plugin_pages_schema_is_discovered_and_does_not_contain_credentials(admin):
    client, _, _ = admin
    data = config(client)
    plugins = {provider["name"]: provider for provider in data["providers"]}
    assert set(plugins) == set(data["available_providers"])
    assert plugins["spotify"]["display_name"] == "Spotify"
    assert "spotdl" in plugins["spotify"]["description"]
    spotify = {field["key"]: field for field in plugins["spotify"]["fields"]}
    assert spotify["SPOTDL_PROVIDER_CREDENTIALS"]["kind"] == "switch"
    assert spotify["SPOTDL_BINARY"]["default"] == "spotdl"
    assert spotify["SPOTIFY_CLIENT_SECRET"]["default"] == ""
    assert plugins["youtube"]["fields"][0]["key"] == "YOUTUBE_API_KEY"
    assert "private-token" not in str(data)
    page = client.get("/admin/").text
    assert 'id="navigation"' in page
    assert 'id="pages"' in page


def test_custom_plugins_expose_their_own_fields_and_legacy_plugins_still_work(admin):
    client, _, _ = admin
    custom = make_provider_class("customuitest", "cui", "Custom Music")
    custom.description = "An independently configured source."
    custom.config_fields = (
        ProviderSetting("CUSTOMUITEST_TOKEN", "Access token"),
        ProviderSetting("CUSTOMUITEST_MODE", "Search mode", kind="select", default="tracks", choices=("tracks", "albums")),
        ProviderSetting("CUSTOMUITEST_LIMIT", "Limit", kind="number", minimum=1, maximum=20, step="1"),
    )
    legacy = make_provider_class("legacyuitest", "lui", "Legacy Music")
    register_provider(custom)
    register_provider(legacy)
    try:
        assert update(client, {
            "CUSTOMUITEST_TOKEN": "custom-secret-value-123", "CUSTOMUITEST_MODE": "albums",
            "LEGACYUITEST_TOKEN": "legacy-hidden",
        }).status_code == 200
        data = config(client)
        plugins = {provider["name"]: provider for provider in data["providers"]}
        assert plugins["customuitest"]["description"] == custom.description
        fields = {field["key"]: field for field in plugins["customuitest"]["fields"]}
        assert fields["CUSTOMUITEST_MODE"]["choices"] == ["tracks", "albums"]
        assert fields["CUSTOMUITEST_LIMIT"]["maximum"] == 20
        assert fields["CUSTOMUITEST_LIMIT"]["step"] == "1"
        assert plugins["legacyuitest"]["fields"] == []
        assert data["values"]["CUSTOMUITEST_MODE"] == "albums"
        assert data["values"]["CUSTOMUITEST_TOKEN"] == ""
        assert data["values"]["LEGACYUITEST_TOKEN"] == ""
        assert "custom-secret-value-123" not in str(data)
        assert "legacy-hidden" not in str(data)
    finally:
        unregister_provider(custom.name)
        unregister_provider(legacy.name)


def test_admin_locked_without_password(tmp_path):
    store = ConfigStore(tmp_path / "config.sqlite3")
    store.initialize({})
    with TestClient(create_app(store=store, enable_cleanup=False), client=("127.0.0.1", 50000)) as client:
        response = client.get("/admin/api/config")
        assert response.status_code == 503
        assert "sudo plex-inject-passwd" in response.json()["detail"]
        response = client.get("/admin/login")
        assert response.status_code == 503
        assert "sudo plex-inject-passwd" in response.text
        assert "python main.py --set-admin-password" in response.text


def test_secrets_redacted_preserved_replaced_and_cleared(admin):
    client, _, store = admin
    data = config(client)
    assert data["values"]["PLEX_TOKEN"] == ""
    assert "PLEX_TOKEN" in data["secrets_set"]
    assert "private-token" not in str(data)
    assert update(client, {"PLEX_TOKEN": "", "SEARCH_LIMIT": "7"}).status_code == 200
    assert store.settings().plex_token == "private-token"
    assert update(client, {"PLEX_TOKEN": "replacement"}).status_code == 200
    assert store.settings().plex_token == "replacement"
    assert update(client, {}, clear=["PLEX_TOKEN"]).status_code == 200
    assert store.settings().plex_token == ""
    assert update(client, {}, clear=["PLEX_URL"]).status_code == 422


def test_save_applies_new_upstream_and_providers_and_survives_restart(admin):
    client, app, store = admin
    old = app.state.runtime
    old.auth_cache["cached"] = 123
    assert update(client, {
        "PLEX_URL": "http://new-plex.test:32400", "ENABLED_PROVIDERS": "",
        "PROXY_PORT": "8123", "SPOTDL_BINARY": "/custom/spotdl", "CUSTOM_TOKEN": "secret",
    }).status_code == 200
    assert client.get("/identity").json()["host"] == "new-plex.test"
    assert not app.state.runtime.auth_cache
    assert not app.state.runtime.registry.providers
    data = config(client)
    assert data["restart_required"]
    assert data["listening_port"] == 32399
    assert data["values"]["CUSTOM_TOKEN"] == ""
    assert "CUSTOM_TOKEN" in data["secrets_set"]
    reopened = ConfigStore(store.path)
    assert reopened.settings().get("SPOTDL_BINARY") == "/custom/spotdl"
    assert reopened.settings().get("CUSTOM_TOKEN") == "secret"
    with TestClient(create_app(store=reopened, enable_cleanup=False), client=("127.0.0.1", 50000)) as restarted:
        assert not config(restarted)["restart_required"]


def test_invalid_or_stale_update_does_not_change_runtime_or_database(admin):
    client, app, store = admin
    old = app.state.runtime
    before = store.values()
    assert update(client, {"PROVIDER_TIMEOUT": "nan"}).status_code == 422
    assert update(client, {"ENABLED_PROVIDERS": "doesnotexist"}).status_code == 422
    assert update(client, {"PROXY_PORT": "70000"}).status_code == 422
    assert update(client, {"INVALID-TOKEN": ""}).status_code == 422
    assert app.state.runtime is old
    assert store.values() == before
    stale = config(client)["revision"]
    assert update(client, {"SEARCH_LIMIT": "9"}).status_code == 200
    response = client.put("/admin/api/config", json={"revision": stale, "values": {"SEARCH_LIMIT": "11"}})
    assert response.status_code == 409
    assert store.settings().search_limit == 9


def test_csrf_and_non_json_requests_rejected(admin):
    client, _, store = admin
    before = store.values()
    assert client.put("/admin/api/config", json={"values": {}}, headers={"Origin": "https://evil.test"}).status_code == 403
    assert client.get("/admin/api/config", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.get("/admin/api/config", headers={"Origin": "https://testserver"}).status_code == 200
    assert client.put("/admin/api/config", content="values=evil").status_code == 415
    assert store.values() == before


def test_password_failures_are_rate_limited(admin):
    client, _, _ = admin
    client.cookies.clear()
    for _ in range(5):
        assert sign_in(client, "wrong").status_code == 401
    response = sign_in(client, "wrong")
    assert response.status_code == 429
    assert response.headers["retry-after"] == "60"


def test_live_update_keeps_old_stream_and_client_alive(tmp_path):
    entered = threading.Event()
    release = threading.Event()

    class SlowBody(_AsyncBody):
        async def __aiter__(self):
            yield self.data
            entered.set()
            while not release.is_set():
                await asyncio.sleep(0.01)

    async def upstream(request):
        body = SlowBody if request.url.path == "/slow" else _AsyncBody
        return httpx.Response(
            200, headers={"Content-Type": "application/json"},
            stream=body(json.dumps({"host": request.url.host}).encode()),
        )

    store = ConfigStore(tmp_path / "config.sqlite3")
    store.initialize({"PLEX_URL": "http://old.test", "DOWNLOAD_DIR": str(tmp_path)})
    store.set_password(AUTH[1])
    app = create_app(store=store, enable_cleanup=False, upstream_transport=httpx.MockTransport(upstream))
    with TestClient(app, client=("127.0.0.1", 50000)) as client, ThreadPoolExecutor(max_workers=1) as pool:
        old = app.state.runtime
        future = pool.submit(client.get, "/slow")
        try:
            assert entered.wait(timeout=5)
            assert update(client, {"PLEX_URL": "http://new.test"}).status_code == 200
            assert not old.http.is_closed
            assert old.requests == 1
            assert client.get("/fast").json()["host"] == "new.test"
        finally:
            release.set()
        assert future.result(timeout=5).json()["host"] == "old.test"
        client.portal.call(old.idle.wait)
        for task in app.state.retired:
            async def wait_retired():
                await task
            client.portal.call(wait_retired)
        assert old.http.is_closed


def test_database_write_failure_is_explicit_and_not_applied(admin, monkeypatch):
    client, app, store = admin
    old = app.state.runtime

    def fail(values):
        import sqlite3
        raise sqlite3.OperationalError("read only")

    monkeypatch.setattr(store, "save", fail)
    response = update(client, {"SEARCH_LIMIT": "7"})
    assert response.status_code == 500
    assert app.state.runtime is old
    assert store.settings().search_limit == 10


def test_search_uses_live_provider_settings(tmp_path, monkeypatch):
    instances = []

    def registry_from_settings(settings):
        provider = FakeProvider(settings, tracks=[track("abc")])
        instances.append(provider)
        return ProviderRegistry([provider]) if settings.enabled_providers else ProviderRegistry()

    monkeypatch.setattr("main.ProviderRegistry.from_settings", registry_from_settings)
    store = ConfigStore(tmp_path / "config.sqlite3")
    store.initialize({"DOWNLOAD_DIR": str(tmp_path)})
    store.set_password(AUTH[1])
    app = create_app(store=store, enable_cleanup=False, upstream_transport=streaming(
        lambda request: httpx.Response(200, json={"MediaContainer": {"size": 0}})
    ))
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        response = client.get("/hubs/search?query=music", headers={"Accept": "application/json"})
        assert response.status_code == 200
        assert instances[-1].searches == [("music", 10)]
        assert update(client, {"SEARCH_LIMIT": "7", "CUSTOM_TOKEN": "new-credential"}).status_code == 200
        assert instances[-1].settings.get("CUSTOM_TOKEN") == "new-credential"
        response = client.get("/hubs/search?query=music", headers={"Accept": "application/json"})
        assert response.status_code == 200
        assert instances[-1].searches == [("music", 7)]
        assert update(client, {"ENABLED_PROVIDERS": ""}).status_code == 200
        response = client.get("/hubs/search?query=music", headers={"Accept": "application/json"})
        assert response.json() == {"MediaContainer": {"size": 0}}
        assert not instances[-1].searches


def test_categories_validate_persist_and_default_port_preserves_existing_databases(admin, tmp_path):
    client, app, store = admin
    data = config(client)
    youtube = next(provider for provider in data["providers"] if provider["name"] == "youtube")
    assert youtube["supported_categories"] == ["music"]
    assert youtube["selected_categories"] == "music"
    assert youtube["dependencies"] == ["yt-dlp", "ffmpeg"]
    assert data["values"]["PROXY_PORT"] == "32399"
    assert update(client, {"YOUTUBE_CATEGORIES": "series"}).status_code == 422
    assert update(client, {"YOUTUBE_CATEGORIES": ""}).status_code == 200
    assert app.state.runtime.registry.get("youtube").enabled_categories == ()
    assert store.settings().env["YOUTUBE_CATEGORIES"] == ""
    assert update(client, {"YOUTUBE_CATEGORIES": "music"}).status_code == 200
    assert app.state.runtime.registry.get("youtube").enabled_categories == ("music",)
    legacy = ConfigStore(tmp_path / "legacy.sqlite3")
    legacy.initialize({"PROXY_PORT": "8080"})
    assert not legacy.initialize({})
    assert legacy.settings().proxy_port == 8080


def test_dependency_api_auth_validation_and_explicit_install(admin, monkeypatch):
    client, app, store = admin
    client.cookies.clear()
    for endpoint in ("check", "install"):
        assert client.post(f"/admin/api/dependencies/spotdl/{endpoint}", json={}).status_code == 401
    sign_in(client)
    assert client.get("/admin/api/dependencies/unknown").status_code == 404
    assert update(client, {"SPOTDL_MODE": "random"}).status_code == 422
    assert client.get("/admin/api/dependencies/spotdl").json() == {"installed": None}
    installed_calls = []

    async def fake_install(settings, name, version):
        installed_calls.append((name, version))
        return {"version": "v4.5.2", "binary": "/private/spotdl", "sha256": "0" * 64}

    async def fake_releases(name, settings):
        assert settings.get("SPOTDL_MODE") == store.settings().get("SPOTDL_MODE")
        return [{"version": "v4.5.2", "published": "2026-07-20"}]

    monkeypatch.setattr(app.state.dependency_manager, "install", fake_install)
    monkeypatch.setattr(app.state.dependency_manager, "releases", fake_releases)
    response = client.post("/admin/api/dependencies/spotdl/check", json={})
    assert response.status_code == 200
    assert response.json()["releases"][0]["version"] == "v4.5.2"
    assert installed_calls == []
    before = app.state.runtime
    response = client.post("/admin/api/dependencies/spotdl/install", json={"version": "v4.5.2"})
    assert response.status_code == 200
    assert installed_calls == [("spotdl", "v4.5.2")]
    assert app.state.runtime is not before
    assert store.values()["SPOTDL_MODE"] == "external"


def test_plex_login_api_saves_masked_token_and_uses_no_account_password(admin):
    client, app, store = admin
    ready = False

    def plex(request):
        if request.method == "POST":
            assert b"password" not in request.content
            return httpx.Response(200, json={"id": 1, "code": "strong", "expiresIn": 600})
        return httpx.Response(200, json={"authToken": "new-plex-token" if ready else None})

    app.state.plex_login.transport = httpx.MockTransport(plex)
    client.cookies.clear()
    assert client.post("/admin/api/plex/login", json={}).status_code == 401
    sign_in(client)
    started = client.post("/admin/api/plex/login", json={}).json()
    assert started["url"].startswith("https://app.plex.tv/auth/")
    payload = {"login_id": started["login_id"]}
    assert not client.post("/admin/api/plex/login/poll", json=payload).json()["complete"]
    ready = True
    response = client.post("/admin/api/plex/login/poll", json=payload)
    assert response.status_code == 200
    assert response.json()["complete"]
    assert response.json()["config"]["values"]["PLEX_TOKEN"] == ""
    assert "new-plex-token" not in response.text
    assert store.settings().plex_token == "new-plex-token"
    assert app.state.settings.plex_token == "new-plex-token"
    assert client.post("/admin/api/plex/login/poll", json=payload).status_code == 409
