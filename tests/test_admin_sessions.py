import re
import time
from dataclasses import replace

from fastapi.testclient import TestClient

from admin_sessions import COOKIE
from tests.test_admin_access import network_app
from tests.test_web_admin import AUTH, sign_in


def test_real_login_form_redirect_cookie_logout(network_app):
    app, _ = network_app
    with TestClient(app, client=("127.0.0.1", 50000), follow_redirects=False) as client:
        assert client.get("/admin/").headers["location"] == "/admin/login"
        page = client.get("/admin/login")
        assert 'method="post" action="/admin/login"' in page.text
        assert "<script" not in page.text
        assert page.headers["cache-control"] == "no-store"
        assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
        response = sign_in(client)
        assert response.status_code == 303
        cookie = response.headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=strict" in cookie and "path=/admin" in cookie
        assert COOKIE in client.cookies
        assert client.get("/admin/").status_code == 200
        saved_cookie = client.cookies.get(COOKIE)
        assert client.post("/admin/api/logout", json={}).status_code == 200
        assert COOKIE not in client.cookies
        client.cookies.set(COOKIE, saved_cookie, path="/admin")
        assert client.get("/admin/api/session").status_code == 401


def test_login_csrf_missing_reused_and_cross_origin(network_app):
    app, _ = network_app
    with TestClient(app, client=("127.0.0.1", 50000), follow_redirects=False) as client:
        assert client.post("/admin/login", data={"username":"admin", "password":AUTH[1]}).status_code == 403
        page = client.get("/admin/login")
        csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        form = {"username":"admin", "password":AUTH[1], "csrf":csrf}
        assert client.post("/admin/login", data=form, headers={"Origin":"http://["}).status_code == 403
        assert client.post("/admin/login", data=form, headers={"Origin":"https://evil.test"}).status_code == 403
        assert client.post("/admin/login", data=form).status_code == 303
        client.cookies.clear()
        assert client.post("/admin/login", data=form).status_code == 403
        assert client.post("/admin/login", json=form).status_code == 415
        assert client.post("/admin/login", content=b"a" * 17000, headers={"Content-Type":"application/x-www-form-urlencoded"}).status_code == 413
        assert client.post("/admin/login", content="username=admin&username=admin", headers={"Content-Type":"application/x-www-form-urlencoded"}).status_code == 400
        token = app.state.admin_sessions.login_token()
        app.state.admin_sessions.login_tokens[app.state.admin_sessions.digest(token)] = time.monotonic() - 1
        assert client.post("/admin/login", data={**form, "csrf":token}).status_code == 403


def test_session_csrf_expiry_and_password_reset(network_app):
    app, store = network_app
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        sign_in(client)
        assert client.post("/admin/api/logout", json={}, headers={"X-CSRF-Token":""}).status_code == 403
        assert client.post("/admin/api/logout", json={}, headers={"X-CSRF-Token":"wrong"}).status_code == 403
        assert client.post("/admin/api/logout", json={}, headers={"Origin":"https://evil.test"}).status_code == 403
        key = app.state.admin_sessions.digest(client.cookies.get(COOKIE))
        session = app.state.admin_sessions.sessions[key]
        assert session.expires > time.monotonic()
        store.set_password("replacement-password-123")
        assert client.get("/admin/api/session").status_code == 401
        client.cookies.clear()
        sign_in(client, "replacement-password-123")
        key = app.state.admin_sessions.digest(client.cookies.get(COOKIE))
        app.state.admin_sessions.sessions[key] = replace(
            app.state.admin_sessions.sessions[key], expires=time.monotonic() - 1,
        )
        assert client.get("/admin/api/session").status_code == 401


def test_https_login_cookie_secure(network_app):
    app, _ = network_app
    with TestClient(app, base_url="https://testserver", client=("127.0.0.1", 50000)) as client:
        response = sign_in(client)
        assert "Secure" in response.headers["set-cookie"]
        assert client.get("/admin/api/session").status_code == 200


def test_basic_auth_no_longer_grants_access(network_app):
    app, _ = network_app
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        assert client.get("/admin/api/config", auth=AUTH).status_code == 401


def test_https_origin_through_tls_proxy_sets_secure_cookie(network_app):
    app, _ = network_app
    with TestClient(app, client=("127.0.0.1", 50000), follow_redirects=False) as client:
        page = client.get("/admin/login")
        token = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        response = client.post("/admin/login", headers={"Origin":"https://testserver"},
            data={"username":"admin", "password":AUTH[1], "csrf":token})
        assert response.status_code == 303
        assert "Secure" in response.headers["set-cookie"]
