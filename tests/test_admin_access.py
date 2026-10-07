import asyncio

import pytest
from fastapi.testclient import TestClient

from admin_access import AdminNetworkMiddleware, allowed_peer, parse_networks
from config_store import ConfigStore
from tests.admin_client import create_app
from tests.test_web_admin import AUTH, config, sign_in, update


@pytest.mark.parametrize("value", ["", "10.1.2.3/8", "localhost", "192.168.0.0/33", "::/129", "10.0.0.0/8,", "0.0.0.0/-1"])
def test_invalid_networks_rejected(value):
    with pytest.raises(ValueError, match="ADMIN_ALLOWED_NETWORKS"):
        parse_networks(value)


@pytest.mark.parametrize("host,allowed", [
    ("192.168.1.10", True), ("192.168.1.255", True), ("192.168.2.10", False),
    ("fd12::1", True), ("2001:db8::1", False), ("::ffff:192.168.1.10", True),
    ("invalid", False), (None, False),
])
def test_network_membership(host, allowed):
    assert allowed_peer(host, parse_networks("192.168.1.0/24,fd00::/8")) is allowed


@pytest.fixture
def network_app(tmp_path):
    store = ConfigStore(tmp_path / "config.sqlite3")
    store.initialize({"RETENTION_DAYS": "0", "ENABLED_PROVIDERS": ""})
    store.set_password(AUTH[1])
    return create_app(store=store, enable_cleanup=False), store


def test_disallowed_networks_block_all_admin_surfaces_before_auth(network_app):
    app, _ = network_app
    with TestClient(app, client=("203.0.113.5", 50000), follow_redirects=False) as client:
        for path in ("/admin", "/admin/", "/admin/login", "/admin/assets/admin.css", "/admin/assets/admin.js", "/admin/api/config", "/admin/api/session", "/admin/unknown"):
            response = client.get(path)
            assert response.status_code == 403
            assert "www-authenticate" not in response.headers
            assert response.headers["cache-control"] == "no-store"
        assert client.post("/admin/login", data={}).status_code == 403
        assert client.put("/admin/api/config", json={}).status_code == 403
        assert client.get("/admin/api/config", headers={
            "X-Forwarded-For": "127.0.0.1", "X-Real-IP": "192.168.1.1",
            "Forwarded": "for=127.0.0.1",
        }).status_code == 403
        # A normal Plex route is not rejected by the admin network policy.
        assert client.get("/identity").status_code == 502


def test_allowlist_applies_live_and_prevents_self_lockout(network_app):
    app, store = network_app
    with TestClient(app, client=("192.168.1.10", 50000)) as client:
        assert sign_in(client).status_code == 303
        before = store.values()
        assert update(client, {"ADMIN_ALLOWED_NETWORKS": "127.0.0.0/8"}).status_code == 422
        assert store.values() == before
        assert update(client, {"ADMIN_ALLOWED_NETWORKS": ""}).status_code == 422
        assert update(client, {"ADMIN_ALLOWED_NETWORKS": "192.168.1.0/24"}).status_code == 200
        assert config(client)["values"]["ADMIN_ALLOWED_NETWORKS"] == "192.168.1.0/24"
        # Reuse the existing app without starting another lifespan.
        other = TestClient(app, client=("127.0.0.1", 50001))
        assert other.get("/admin/login").status_code == 403
    assert ConfigStore(store.path).values()["ADMIN_ALLOWED_NETWORKS"] == "192.168.1.0/24"


def test_websocket_denied_without_a_client_address(network_app):
    app, _ = network_app
    async def exercise():
        sent = []
        async def never_called(scope, receive, send):
            pytest.fail("Blocked websocket reached downstream")
        async def receive():
            return {"type": "websocket.connect"}
        async def send(message):
            sent.append(message)
        await AdminNetworkMiddleware(never_called)(
            {"type": "websocket", "path": "/admin/stream", "app": app, "client": None},
            receive, send,
        )
        assert sent == [{"type": "websocket.close", "code": 1008}]
    asyncio.run(exercise())


def test_cli_network_recovery(network_app, monkeypatch, capsys):
    from main import run
    _, store = network_app
    monkeypatch.setenv("CONFIG_DB", str(store.path))
    monkeypatch.setattr("sys.argv", ["main.py", "--set-admin-networks", "127.0.0.0/8,::1/128"])
    run()
    assert store.values()["ADMIN_ALLOWED_NETWORKS"] == "127.0.0.0/8,::1/128"
    assert "Restart" in capsys.readouterr().out
