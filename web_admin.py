"""Password-protected configuration API and a small local-assets-only web UI."""

from __future__ import annotations

import asyncio
import logging
import secrets
import sqlite3
import time
import html
import hmac
from dataclasses import asdict
import httpx
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, ConfigDict, Field, StrictStr

from config_store import ConfigStore, is_secret, validate, validate_key
from providers import ProviderRegistry, discover_providers
from dependencies import DependencyError, DependencyManager, TOOLS, installed
from plex_login import PlexLogin, PlexLoginError
from admin_access import allowed_peer, parse_networks
from admin_sessions import AdminSessions, COOKIE, SESSION_SECONDS
from plex_libraries import PlexLibraryError, music_libraries

logger = logging.getLogger(__name__)
ASSETS = Path(__file__).parent / "web"


class ConfigUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    values: dict[str, StrictStr]
    revision: str
    clear: list[StrictStr] = Field(default_factory=list)

class ToolInstall(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: StrictStr = "latest"


class LoginPoll(BaseModel):
    model_config = ConfigDict(extra="forbid")
    login_id: StrictStr


def install_admin(app: FastAPI, store: ConfigStore, listening_port: int, admin_port: int) -> None:
    auth_lock = asyncio.Lock()
    failures: dict[str, tuple[int, float]] = {}
    revision = secrets.token_hex(16)
    manager = DependencyManager()
    login = PlexLogin()
    app.state.dependency_manager = manager
    app.state.plex_login = login
    sessions = AdminSessions()
    app.state.admin_sessions = sessions
    page_headers = {
        "Cache-Control": "no-store",
        "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "same-origin",
    }

    def validate_plugins(values: dict[str, str]) -> None:
        for name, provider in discover_providers().items():
            key = f"{name.upper()}_CATEGORIES"
            if key in values:
                selected = {category.strip() for category in values[key].split(",") if category.strip()}
                unsupported = selected - set(provider.supported_categories)
                if unsupported:
                    raise HTTPException(422, f"{provider.display_name} does not support: {', '.join(sorted(unsupported))}")
    async def persist(values: dict[str, str]) -> None:
        nonlocal revision
        validate_plugins(values)
        try:
            settings = validate(values)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        classes = discover_providers()
        unknown = set(settings.enabled_providers) - classes.keys()
        if unknown:
            raise HTTPException(422, f"Unknown providers: {', '.join(sorted(unknown))}")
        registry = ProviderRegistry.from_settings(settings)
        try:
            store.save(values)
        except sqlite3.Error as exc:
            logger.exception("Failed to save configuration")
            raise HTTPException(500, "Database write failed; configuration was not applied") from exc
        await app.state.apply_settings(settings, registry)
        revision = secrets.token_hex(16)

    def same_origin(request: Request) -> None:
        origin = request.headers.get("origin")
        if origin:
            try:
                parsed = urlsplit(origin)
            except ValueError as exc:
                raise HTTPException(403, "Invalid administration origin") from exc
            # TLS termination can change the transport scheme, but must preserve Host.
            schemes = {request.url.scheme}
            if request.url.scheme == "http":
                schemes.add("https")
            if parsed.scheme not in schemes or parsed.netloc != request.url.netloc:
                raise HTTPException(403, "Cross-origin administration is not allowed")
        if request.headers.get("sec-fetch-site") == "cross-site":
            raise HTTPException(403, "Cross-site administration is not allowed")

    async def authorized(request: Request) -> None:
        password_hash = store.password_hash()
        if password_hash is None:
            raise HTTPException(503, "Admin interface locked. Run sudo plex-inject-passwd (RPM) or python main.py --set-admin-password (source checkout) on the server.")
        same_origin(request)
        session = sessions.get(request, password_hash)
        if session is None:
            raise HTTPException(401, "Administrator login required")
        if request.method not in ("GET", "HEAD"):
            if not hmac.compare_digest(request.headers.get("x-csrf-token", "").encode(), session.csrf.encode()):
                raise HTTPException(403, "Invalid session CSRF token")
            if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
                raise HTTPException(415, "Administration writes require application/json")

    def login_page(error: str = "", status_code: int = 200) -> Response:
        content = (ASSETS / "login.html").read_text().replace(
            "__CSRF__", sessions.login_token(),
        ).replace("__ERROR__", html.escape(error))
        return HTMLResponse(content, status_code=status_code, headers=page_headers)

    @app.get("/admin/login")
    async def get_login(request: Request) -> Response:
        same_origin(request)
        if store.password_hash() is None:
            return login_page("Admin interface locked. Run sudo plex-inject-passwd (RPM) or python main.py --set-admin-password (source checkout) on the server.", 503)
        if sessions.get(request, store.password_hash()) is not None:
            return RedirectResponse("/admin/", status_code=303, headers={"Cache-Control": "no-store"})
        return login_page()

    @app.post("/admin/login")
    async def submit_login(request: Request) -> Response:
        same_origin(request)
        if store.password_hash() is None:
            return login_page("Admin interface locked. Run sudo plex-inject-passwd (RPM) or python main.py --set-admin-password (source checkout) on the server.", 503)
        if request.headers.get("content-type", "").split(";")[0] != "application/x-www-form-urlencoded":
            raise HTTPException(415, "Login requires a form submission")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 16384:
                raise HTTPException(413, "Login form is too large")
        try:
            fields = parse_qs(body.decode("utf-8"), keep_blank_values=True, max_num_fields=8)
        except (UnicodeDecodeError, ValueError) as exc:
            raise HTTPException(400, "Invalid login form") from exc
        if any(len(values) != 1 for values in fields.values()):
            raise HTTPException(400, "Duplicate login form fields")
        csrf = fields.get("csrf", [""])[0]
        if not sessions.consume_login_token(csrf):
            return login_page("Login form expired. Please try again.", 403)
        username = fields.get("username", [""])[0]
        password = fields.get("password", [""])[0]
        host = request.client.host if request.client else "unknown"
        async with auth_lock:
            now = time.monotonic()
            for key in [key for key, (_, expiry) in failures.items() if expiry <= now]:
                del failures[key]
            count, expiry = failures.get(host, (0, now + 60))
            if count >= 5:
                response = login_page("Too many failed logins. Try again in one minute.", 429)
                response.headers["Retry-After"] = "60"
                return response
            checked_hash = store.password_hash()
            correct = await asyncio.to_thread(store.verify_password, password)
            correct = correct and checked_hash == store.password_hash()
            if username != "admin" or not correct:
                failures[host] = (count + 1, expiry)
                logger.warning("Failed administrator login from %s", host)
                return login_page("Invalid username or password.", 401)
            failures.pop(host, None)
            password_hash = store.password_hash()
            if password_hash is None:
                raise HTTPException(503, "Admin password is not configured")
            sessions.remove(request)
            secure = request.url.scheme == "https" or request.headers.get("origin", "").startswith("https://")
            token, _ = sessions.create(password_hash, secure)
        response = RedirectResponse("/admin/", status_code=303, headers={"Cache-Control": "no-store"})
        response.set_cookie(COOKIE, token, max_age=SESSION_SECONDS, httponly=True, samesite="strict", secure=secure, path="/admin")
        logger.info("Administrator signed in from %s", host)
        return response

    @app.post("/admin/api/logout", dependencies=[Depends(authorized)])
    async def logout(request: Request) -> Response:
        session = sessions.get(request, store.password_hash())
        sessions.remove(request)
        response = JSONResponse({"logged_out": True}, headers={"Cache-Control": "no-store"})
        response.delete_cookie(COOKIE, path="/admin", httponly=True, samesite="strict", secure=session.secure if session else False)
        return response

    @app.get("/admin/api/session", dependencies=[Depends(authorized)])
    async def session_info(request: Request) -> Response:
        session = sessions.get(request, store.password_hash())
        if session is None:
            raise HTTPException(401, "Administrator login required")
        return JSONResponse({"csrf": session.csrf}, headers={"Cache-Control": "no-store"})

    def snapshot() -> dict:
        values = store.values()
        classes = discover_providers()
        return {
            "values": {key: "" if is_secret(key) else value for key, value in values.items()},
            "secrets_set": [key for key, value in values.items() if is_secret(key) and value],
            "revision": revision,
            "available_providers": sorted(classes),
            "providers": [
                {
                    "name": name,
                    "display_name": provider.display_name,
                    "description": provider.description,
                    "fields": [asdict(field) for field in provider.config_fields],
                    "supported_categories": list(provider.supported_categories),
                    "selected_categories": values.get(f"{name.upper()}_CATEGORIES", ",".join(provider.supported_categories)),
                    "dependencies": list(provider.dependencies),
                }
                for name, provider in sorted(classes.items())
            ],
            "active_providers": [provider.name for provider in app.state.runtime.registry.providers],
            "restart_required": app.state.settings.proxy_port != listening_port or app.state.settings.admin_port != admin_port,
            "listening_port": listening_port,
            "admin_listening_port": admin_port,
            "dependency_tools": [
                {
                    "name": tool.name, "source": tool.repository, "mode_key": tool.mode_key,
                    "binary_key": tool.binary_key, "modes": list(tool.modes),
                }
                for tool in TOOLS.values()
            ],
        }

    async def admin_page(request: Request) -> Response:
        same_origin(request)
        if sessions.get(request, store.password_hash()) is None:
            return RedirectResponse("/admin/login", status_code=303, headers={"Cache-Control": "no-store"})
        return FileResponse(ASSETS / "index.html", headers=page_headers)

    app.add_api_route("/admin", admin_page, methods=["GET"])
    app.add_api_route("/admin/", admin_page, methods=["GET"])

    @app.get("/admin/assets/{name}")
    async def asset(name: str, request: Request) -> Response:
        if name != "admin.css":
            await authorized(request)
        if name not in ("admin.js", "admin.css"):
            raise HTTPException(404, "Unknown asset")
        return FileResponse(ASSETS / name, headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    @app.get("/admin/api/config", dependencies=[Depends(authorized)])
    async def get_config() -> Response:
        return JSONResponse(snapshot(), headers={"Cache-Control": "no-store"})

    @app.get("/admin/api/plex/music-libraries", dependencies=[Depends(authorized)])
    async def get_music_libraries() -> Response:
        settings = app.state.settings
        try:
            libraries = await asyncio.to_thread(music_libraries, settings)
        except PlexLibraryError as exc:
            logger.warning("Plex music-library discovery failed: %s", exc)
            raise HTTPException(502, str(exc)) from exc
        return JSONResponse({"libraries": libraries}, headers={"Cache-Control": "no-store"})

    @app.get("/admin/api/logs", dependencies=[Depends(authorized)])
    async def get_logs(
        limit: int = Query(100, ge=1, le=200), before: int | None = Query(None, ge=1, le=2**63 - 1),
        level: Literal["INFO", "WARNING", "ERROR", "CRITICAL"] | None = None,
        category: Literal["search", "download", "system"] | None = None,
    ) -> Response:
        try:
            entries = app.state.activity_log.entries(limit, before, level, category)
        except sqlite3.Error as exc:
            logger.exception("Failed to read activity logs")
            raise HTTPException(500, "Unable to read activity logs") from exc
        return JSONResponse({"entries": entries, "retained_limit": 2000}, headers={"Cache-Control": "no-store"})

    @app.put("/admin/api/config", dependencies=[Depends(authorized)])
    async def put_config(update: ConfigUpdate, request: Request) -> Response:
        nonlocal revision
        async with app.state.config_lock:
            if update.revision != revision:
                raise HTTPException(409, "Configuration changed; reload before saving")
            values = store.values()
            for key, value in update.values.items():
                try:
                    validate_key(key)
                except ValueError as exc:
                    raise HTTPException(422, str(exc)) from exc
                if is_secret(key) and value == "":
                    continue
                values[key] = value.strip()
            for key in update.clear:
                if key not in values or not is_secret(key):
                    raise HTTPException(422, f"Cannot clear secret {key}")
                values[key] = ""
            try:
                networks = parse_networks(values["ADMIN_ALLOWED_NETWORKS"])
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            if not allowed_peer(request.client.host if request.client else None, networks):
                raise HTTPException(422, "The allowlist must include your current peer address to prevent lockout. Use --set-admin-networks on the server for recovery.")
            await persist(values)
            logger.info("Administrator saved and applied configuration")
            return JSONResponse(snapshot(), headers={"Cache-Control": "no-store"})

    def known_tool(name: str) -> None:
        if name not in TOOLS or not any(name in provider.dependencies for provider in discover_providers().values()):
            raise HTTPException(404, "Unknown plugin dependency")

    @app.get("/admin/api/dependencies/{name}", dependencies=[Depends(authorized)])
    async def dependency_status(name: str) -> Response:
        known_tool(name)
        try:
            info = installed(store.settings(), name)
        except DependencyError as exc:
            raise HTTPException(500, str(exc)) from exc
        return JSONResponse({"installed": info}, headers={"Cache-Control": "no-store"})

    @app.post("/admin/api/dependencies/{name}/check", dependencies=[Depends(authorized)])
    async def check_dependency(name: str) -> Response:
        known_tool(name)
        try:
            releases = await manager.releases(name)
        except (httpx.HTTPError, ValueError, KeyError, DependencyError) as exc:
            logger.warning("Failed to check %s releases: %s", name, type(exc).__name__)
            raise HTTPException(502, f"Could not retrieve {name} releases from upstream") from exc
        return JSONResponse({"releases": releases}, headers={"Cache-Control": "no-store"})

    @app.post("/admin/api/dependencies/{name}/install", dependencies=[Depends(authorized)])
    async def install_dependency(name: str, update: ToolInstall) -> Response:
        known_tool(name)
        async with app.state.config_lock:
            try:
                info = await manager.install(store.settings(), name, update.version)
            except (DependencyError, httpx.HTTPError, OSError, ValueError, KeyError, asyncio.TimeoutError) as exc:
                logger.warning("Dependency installation failed for %s: %s", name, type(exc).__name__)
                raise HTTPException(502, str(exc) if isinstance(exc, DependencyError) else f"Could not install {name}; previous installation remains selected") from exc
            await app.state.apply_settings(store.settings(), ProviderRegistry.from_settings(store.settings()))
            return JSONResponse({"installed": info}, headers={"Cache-Control": "no-store"})

    @app.post("/admin/api/plex/login", dependencies=[Depends(authorized)])
    async def start_plex_login() -> Response:
        try:
            result = await login.start(revision)
        except (httpx.HTTPError, ValueError, KeyError, PlexLoginError) as exc:
            logger.warning("Plex login start failed: %s", type(exc).__name__)
            raise HTTPException(502, str(exc) if isinstance(exc, PlexLoginError) else "Plex authorization service unavailable") from exc
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @app.post("/admin/api/plex/login/poll", dependencies=[Depends(authorized)])
    async def poll_plex_login(poll: LoginPoll) -> Response:
        async with app.state.config_lock:
            try:
                token = await login.poll(poll.login_id, revision)
            except PlexLoginError as exc:
                raise HTTPException(409, str(exc)) from exc
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                logger.warning("Plex login check failed: %s", type(exc).__name__)
                raise HTTPException(502, "Could not check Plex authorization") from exc
            if token is None:
                return JSONResponse({"complete": False}, headers={"Cache-Control": "no-store"})
            values = store.values()
            values["PLEX_TOKEN"] = token
            await persist(values)
            return JSONResponse({"complete": True, "config": snapshot()}, headers={"Cache-Control": "no-store"})

    @app.api_route("/admin/{rest:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
    async def unknown_admin() -> Response:
        raise HTTPException(404, "Unknown administration route")
