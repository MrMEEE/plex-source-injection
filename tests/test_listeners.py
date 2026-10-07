import asyncio
import socket

import httpx
import pytest
from fastapi.testclient import TestClient

from config_store import ConfigStore
from listeners import serve
from main import create_app
from tests.test_main import streaming
from tests.test_web_admin import AUTH, sign_in, update


def test_admin_routes_are_not_present_on_proxy_listener(tmp_path):
    store = ConfigStore(tmp_path / "config.sqlite3")
    store.initialize({"ENABLED_PROVIDERS": "", "RETENTION_DAYS": "0"})
    store.set_password(AUTH[1])
    requests = []
    def upstream(request):
        requests.append(request.url.path)
        return httpx.Response(200, json={"upstream": True})
    proxy = create_app(store=store, upstream_transport=streaming(upstream), enable_cleanup=False)
    with TestClient(proxy, client=("127.0.0.1", 50000)) as plex:
        admin = TestClient(proxy.state.admin_app, client=("127.0.0.1", 50000))
        admin.portal = plex.portal
        assert plex.get("/identity").status_code == 200
        for path in ("/admin", "/admin/", "/admin/login", "/admin/api/logs", "/admin/api/config", "/admin/assets/admin.css", "/admin/unknown"):
            for method in ("GET", "POST", "OPTIONS"):
                assert plex.request(method, path).status_code == 404
        assert requests == ["/identity"]
        assert admin.get("/identity").status_code == 404
        assert sign_in(admin).status_code == 303
        assert admin.get("/admin/api/config").json()["admin_listening_port"] == 32300
        assert update(admin, {"ADMIN_PORT": "32301"}).status_code == 200
        saved = admin.get("/admin/api/config").json()
        assert saved["restart_required"]
        assert saved["admin_listening_port"] == 32300
        assert store.settings().admin_port == 32301
        admin.close()


def test_admin_port_conflict_releases_proxy_socket(tmp_path):
    store = ConfigStore(tmp_path / "config.sqlite3")
    store.initialize({"ENABLED_PROVIDERS": "", "RETENTION_DAYS": "0"})
    app = create_app(store=store, enable_cleanup=False)
    with socket.socket() as busy:
        busy.bind(("0.0.0.0", 0))
        busy.listen()
        admin_port = busy.getsockname()[1]
        with socket.socket() as free:
            free.bind(("0.0.0.0", 0))
            proxy_port = free.getsockname()[1]
        with pytest.raises(OSError):
            asyncio.run(serve(app, proxy_port, admin_port))
        with socket.socket() as probe:
            probe.bind(("0.0.0.0", proxy_port))
    assert not hasattr(app.state, "runtime")


def test_equal_listener_ports_rejected(tmp_path):
    store = ConfigStore(tmp_path / "config.sqlite3")
    store.initialize({"ENABLED_PROVIDERS": ""})
    with pytest.raises(ValueError, match="different"):
        asyncio.run(serve(create_app(store=store), 32399, 32399))
