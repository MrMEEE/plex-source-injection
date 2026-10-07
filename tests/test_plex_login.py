import asyncio
import time

import httpx
import pytest

from plex_login import PlexLogin, PlexLoginError


def test_plex_pin_flow_no_password_and_no_token_before_authorized():
    calls = []
    ready = False

    def upstream(request):
        calls.append(request)
        if request.method == "POST":
            assert b"strong=true" in request.content
            return httpx.Response(200, json={"id": 42, "code": "strong-code", "expiresIn": 600})
        assert request.url.params["code"] == "strong-code"
        return httpx.Response(200, json={"authToken": "authorized-token" if ready else None})

    login = PlexLogin(httpx.MockTransport(upstream))
    started = asyncio.run(login.start("revision"))
    assert started["url"].startswith("https://app.plex.tv/auth/#!?")
    assert "strong-code" in started["url"]
    assert asyncio.run(login.poll(started["login_id"], "revision")) is None
    ready = True
    assert asyncio.run(login.poll(started["login_id"], "revision")) == "authorized-token"
    with pytest.raises(PlexLoginError, match="expired"):
        asyncio.run(login.poll(started["login_id"], "revision"))
    assert calls[0].headers["X-Plex-Client-Identifier"] == calls[1].headers["X-Plex-Client-Identifier"]


def test_pin_expiration_and_revision_changes():
    login = PlexLogin(httpx.MockTransport(lambda request: httpx.Response(200, json={"id": 1, "code": "pin", "expiresIn": 600})))
    started = asyncio.run(login.start("old"))
    with pytest.raises(PlexLoginError, match="Configuration changed"):
        asyncio.run(login.poll(started["login_id"], "new"))
    started = asyncio.run(login.start("new"))
    login.pins[started["login_id"]].expires = time.monotonic() - 1
    with pytest.raises(PlexLoginError, match="expired"):
        asyncio.run(login.poll(started["login_id"], "new"))
