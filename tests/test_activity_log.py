import asyncio
import logging
import sqlite3

import httpx
from fastapi.testclient import TestClient

from activity_log import MAX_ENTRIES, ActivityLog
from config_store import ConfigStore
from providers import ProviderRegistry
from tests.admin_client import create_app
from tests.conftest import FakeProvider, track
from tests.test_main import streaming
from tests.test_web_admin import AUTH, admin, sign_in


def test_logs_authentication_filters_and_no_cache(admin):
    client, app, _ = admin
    logging.getLogger("search").info("Found Song [ext_fk_song]")
    logging.getLogger("ingest").error("Download failed")
    response = client.get("/admin/api/logs?category=download&level=ERROR")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["entries"][0]["message"] == "Download failed"
    assert all(entry["category"] == "download" for entry in response.json()["entries"])
    newest = client.get("/admin/api/logs?limit=1").json()["entries"][0]
    older = client.get(f"/admin/api/logs?before={newest['id']}").json()["entries"]
    assert all(entry["id"] < newest["id"] for entry in older)
    for query in ("limit=201", "limit=0", "before=-1", "before=999999999999999999999", "category=invalid", "level=DEBUG"):
        assert client.get("/admin/api/logs?" + query).status_code == 422
    client.cookies.clear()
    assert client.get("/admin/api/logs").status_code == 401


def test_redaction_persistence_and_exact_retention_limit(tmp_path):
    store = ConfigStore(tmp_path / "config.sqlite3")
    store.initialize({})
    values = {"PLEX_TOKEN": "private-plex-token", "SPOTIFY_CLIENT_SECRET": "client-secret"}
    log = ActivityLog(store, lambda: values)
    with store.connect() as db:
        db.executemany(
            "INSERT INTO activity_log(timestamp,level,category,source,message) VALUES (?,?,?,?,?)",
            [("2026-10-07", "INFO", "search", "search", str(i)) for i in range(MAX_ENTRIES + 1)],
        )
    record = logging.LogRecord("ingest", logging.ERROR, "", 1,
        "private-plex-token client-secret http://user:pass@plex.test/scan?X-Plex-Token=encoded-secret token=unknown-secret", (), None)
    log.emit(record)
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM activity_log").fetchone()[0] == MAX_ENTRIES
        assert db.execute("SELECT MIN(id) FROM activity_log").fetchone()[0] == 3
        saved = db.execute("SELECT message FROM activity_log ORDER BY id DESC LIMIT 1").fetchone()[0]
    for secret in ("private-plex-token", "client-secret", "user:pass", "encoded-secret", "unknown-secret"):
        assert secret not in saved
    assert "[redacted]" in saved
    reopened = ActivityLog(ConfigStore(store.path), lambda: values)
    assert reopened.entries(1, None, "ERROR", "download")[0]["message"] == saved
    long = logging.LogRecord("search", logging.INFO, "", 1, "x" * 5000, (), None)
    reopened.emit(long)
    assert len(reopened.entries(1, None, None, None)[0]["message"]) == 4000
    log.close()
    reopened.close()


def test_unrelated_http_library_logs_are_not_recorded(admin):
    client, _, _ = admin
    logging.getLogger("httpx").warning("GET http://server?secret=not-for-admin")
    assert all(entry["source"] != "httpx" for entry in client.get("/admin/api/logs").json()["entries"])


def test_search_download_registration_and_errors_are_recorded(tmp_path):
    store = ConfigStore(tmp_path / "config.sqlite3")
    store.initialize({"DOWNLOAD_DIR": str(tmp_path / "music"), "RETENTION_DAYS": "0"})
    store.set_password(AUTH[1])
    settings = store.settings()
    provider = FakeProvider(settings, tracks=[track("song"), track("broken")])
    indexed = False

    def upstream(request):
        nonlocal indexed
        if request.url.path.endswith("/refresh"):
            indexed = True
        return httpx.Response(200, json={"MediaContainer": {"size": 0}})

    app = create_app(settings=settings, store=store, registry=ProviderRegistry([provider]),
        upstream_transport=streaming(upstream), enable_cleanup=False)
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        app.state.ingestor._lookup = lambda item_id, title: "123" if indexed else None
        sign_in(client)
        assert client.get("/hubs/search?query=example").status_code == 200
        assert client.get("/library/metadata/ext_fk_song").status_code == 200
        indexed = False
        provider.error = RuntimeError("downloader unavailable")
        assert client.get("/library/metadata/ext_fk_broken").status_code == 502
        assert client.get("/hubs/search?query=failing").status_code == 200
        entries = client.get("/admin/api/logs").json()["entries"]
        messages = "\n".join(entry["message"] for entry in entries)
        assert "example" in messages and "Song [ext_fk_song]" in messages
        assert "Downloading ext_fk_song" in messages
        assert "Registered ext_fk_song in Plex" in messages
        assert "Ingest failed" in messages and "downloader unavailable" in messages
        assert any(entry["category"] == "download" and entry["level"] == "ERROR" for entry in entries)
        assert any(entry["category"] == "search" and entry["level"] == "ERROR" for entry in entries)
    assert app.state.activity_log not in logging.getLogger().handlers


def test_log_database_errors_are_explicit(admin, monkeypatch):
    client, app, _ = admin
    def fail(*args):
        raise sqlite3.OperationalError("read failed")
    monkeypatch.setattr(app.state.activity_log, "entries", fail)
    response = client.get("/admin/api/logs")
    assert response.status_code == 500
    assert response.json()["detail"] == "Unable to read activity logs"


def test_disconnected_download_failure_is_logged(admin):
    client, app, _ = admin
    async def exercise():
        started, finish = asyncio.Event(), asyncio.Event()
        async def fail(external_id):
            started.set()
            await finish.wait()
            raise RuntimeError("failed after disconnect")
        app.state.ingestor._ingest = fail
        caller = asyncio.create_task(app.state.ingestor.download_and_register("ext_fk_background"))
        await started.wait()
        caller.cancel()
        try:
            await caller
        except asyncio.CancelledError:
            pass
        finish.set()
        while app.state.ingestor._inflight:
            await asyncio.sleep(0)
    client.portal.call(exercise)
    messages = [entry["message"] for entry in client.get("/admin/api/logs?category=download").json()["entries"]]
    assert any("ext_fk_background failed: failed after disconnect" in message for message in messages)
