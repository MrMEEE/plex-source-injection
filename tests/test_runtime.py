import asyncio
import httpx
from fastapi.testclient import TestClient

from config_store import ConfigStore
from ingest import Ingestor
from tests.admin_client import create_app
from providers import ProviderRegistry
from runtime import Runtime
from tests.test_main import streaming
from tests.test_web_admin import AUTH, update


def test_retired_runtime_waits_for_shielded_download(settings):
    async def exercise():
        ingestor = Ingestor(settings, ProviderRegistry())
        runtime = Runtime(settings, ProviderRegistry(), ingestor=ingestor)
        ingestor._http = runtime.http
        started = asyncio.Event()
        finish = asyncio.Event()

        async def ingest(external_id):
            started.set()
            await finish.wait()
            assert not runtime.http.is_closed
            return "123"

        ingestor._ingest = ingest
        caller = asyncio.create_task(ingestor.download_and_register("ext_fk_abc"))
        await started.wait()
        caller.cancel()
        try:
            await caller
        except asyncio.CancelledError:
            pass
        closing = asyncio.create_task(runtime.close())
        await asyncio.sleep(0)
        assert not closing.done()
        assert not runtime.http.is_closed
        finish.set()
        await closing
        assert runtime.http.is_closed

    asyncio.run(exercise())

def test_generations_share_existing_download_for_unchanged_library(settings):
    async def exercise():
        old = Ingestor(settings, ProviderRegistry())
        new = Ingestor(settings, ProviderRegistry())
        started = asyncio.Event()
        finish = asyncio.Event()
        calls = []

        async def ingest(external_id):
            calls.append(external_id)
            started.set()
            await finish.wait()
            return "123"

        old._ingest = ingest
        first = asyncio.create_task(old.download_and_register("ext_fk_abc"))
        await started.wait()
        new.share_download_state(old)
        second = asyncio.create_task(new.download_and_register("ext_fk_abc"))
        await asyncio.sleep(0)
        finish.set()
        assert await first == await second == "123"
        assert calls == ["ext_fk_abc"]
        assert await new.download_and_register("ext_fk_abc") == "123"

    asyncio.run(exercise())


def test_cleanup_rescheduled_and_disabled_on_save(tmp_path, monkeypatch):
    runs = []
    stopped = []

    async def cleanup(path, days, interval, on_deleted):
        runs.append((path, days, interval))
        try:
            await asyncio.Event().wait()
        finally:
            stopped.append(path)

    monkeypatch.setattr("runtime.run_cleanup_loop", cleanup)
    store = ConfigStore(tmp_path / "config.sqlite3")
    store.initialize({"DOWNLOAD_DIR": str(tmp_path)})
    store.set_password(AUTH[1])
    app = create_app(store=store, upstream_transport=streaming(
        lambda request: httpx.Response(200)
    ))
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        client.get("/identity")
        assert runs == [(tmp_path, 30, 86400)]
        assert update(client, {
            "DOWNLOAD_DIR": str(tmp_path / "new"), "RETENTION_DAYS": "7",
            "CLEANUP_INTERVAL_HOURS": "2",
        }).status_code == 200
        client.get("/identity")
        assert runs[-1] == (tmp_path / "new", 7, 7200)
        assert stopped == [tmp_path]
        assert update(client, {"RETENTION_DAYS": "0"}).status_code == 200
        assert app.state.runtime.cleanup_task is None
        assert stopped == [tmp_path, tmp_path / "new"]
