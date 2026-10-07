"""Network policy for every administration route, using the direct ASGI peer."""

from __future__ import annotations

import ipaddress
import logging

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

DEFAULT_ADMIN_NETWORKS = "127.0.0.0/8,::1/128,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,fc00::/7"
Network = ipaddress.IPv4Network | ipaddress.IPv6Network
logger = logging.getLogger(__name__)


def parse_networks(value: str) -> tuple[Network, ...]:
    entries = value.split(",")
    if not entries or any(not entry.strip() for entry in entries):
        raise ValueError("ADMIN_ALLOWED_NETWORKS requires a non-empty comma-separated list of IP addresses or CIDR networks")
    try:
        return tuple(ipaddress.ip_network(entry.strip(), strict=True) for entry in entries)
    except ValueError as exc:
        raise ValueError(f"ADMIN_ALLOWED_NETWORKS: invalid IP address or canonical CIDR network ({exc})") from exc


def allowed_peer(host: str | None, networks: tuple[Network, ...]) -> bool:
    if host is None:
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return any(address.version == network.version and address in network for network in networks)


class AdminNetworkMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] in ("http", "websocket") and (path.rstrip("/") == "/admin" or path.startswith("/admin/")):
            settings = scope["app"].state.settings
            networks = parse_networks(settings.env.get("ADMIN_ALLOWED_NETWORKS", DEFAULT_ADMIN_NETWORKS))
            peer = scope.get("client")
            host = peer[0] if peer else None
            if not allowed_peer(host, networks):
                logger.warning("Blocked administration request from disallowed peer %s", host)
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                else:
                    response = JSONResponse(
                        {"detail": "Your network is not allowed to access administration"},
                        status_code=403, headers={"Cache-Control": "no-store"},
                    )
                    await response(scope, receive, send)
                return
        await self.app(scope, receive, send)
