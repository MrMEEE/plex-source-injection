"""Short-lived Plex PIN authorization; account passwords never enter the proxy."""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx


class PlexLoginError(RuntimeError):
    pass


@dataclass
class Pin:
    pin_id: int
    code: str
    client_id: str
    expires: float
    revision: str


class PlexLogin:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.transport = transport
        self.pins: dict[str, Pin] = {}

    def headers(self, client_id: str) -> dict[str, str]:
        return {
            "Accept": "application/json", "X-Plex-Client-Identifier": client_id,
            "X-Plex-Product": "Plex Source Injection", "X-Plex-Version": "1",
        }

    async def start(self, revision: str) -> dict[str, str]:
        now = time.monotonic()
        self.pins = {key: pin for key, pin in self.pins.items() if pin.expires > now}
        if len(self.pins) >= 5:
            raise PlexLoginError("Too many pending logins. Wait for an existing login to expire.")
        client_id = secrets.token_hex(16)
        async with httpx.AsyncClient(transport=self.transport, timeout=15) as client:
            response = await client.post(
                "https://plex.tv/api/v2/pins", headers=self.headers(client_id), data={"strong": "true"},
            )
            response.raise_for_status()
            data = response.json()
        login_id = secrets.token_urlsafe(32)
        self.pins[login_id] = Pin(int(data["id"]), data["code"], client_id, now + min(int(data["expiresIn"]), 600), revision)
        return {
            "login_id": login_id,
            "url": "https://app.plex.tv/auth/#!?" + urlencode({
                "clientID": client_id, "code": data["code"],
                "context[device][product]": "Plex Source Injection",
            }),
        }

    async def poll(self, login_id: str, revision: str) -> str | None:
        pin = self.pins.get(login_id)
        if not pin or pin.expires <= time.monotonic():
            self.pins.pop(login_id, None)
            raise PlexLoginError("Login expired. Start again.")
        if revision != pin.revision:
            self.pins.pop(login_id, None)
            raise PlexLoginError("Configuration changed during login. Reload and start again.")
        async with httpx.AsyncClient(transport=self.transport, timeout=15) as client:
            response = await client.get(
                f"https://plex.tv/api/v2/pins/{pin.pin_id}",
                headers=self.headers(pin.client_id), params={"code": pin.code},
            )
            response.raise_for_status()
            data = response.json()
        token = data.get("authToken")
        if token:
            self.pins.pop(login_id, None)
            return token
        return None
