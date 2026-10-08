"""FastAPI reverse proxy sitting between Plex clients and Plex Media Server."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable
from urllib.parse import parse_qsl, urlencode

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask
from starlette.requests import ClientDisconnect

from config import Settings
from config_store import ConfigStore
from ingest import IngestError, Ingestor, ItemNotFoundError
from providers import ProviderError, ProviderRegistry, is_external_id
from search import inject_results, search_external
from runtime import Runtime, RuntimeMiddleware
from web_admin import install_admin
from admin_access import AdminNetworkMiddleware, parse_networks
from activity_log import ActivityLog

logger = logging.getLogger("plex_proxy")

ALL_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
# Headers that must not be forwarded by a proxy (RFC 7230 §6.1) plus ones httpx recomputes.
HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "trailers",
    "transfer-encoding",
    "upgrade",
}
REQUEST_EXCLUDED = HOP_BY_HOP | {"host", "content-length"}
EMBEDDED_EXTERNAL_RE = re.compile(r"(/library/metadata/)(ext_[a-z0-9]+_[A-Za-z0-9_-]+)")
AUTH_CACHE_TTL = 300.0


def _raw_path(request: Request) -> str:
    """Return the request path exactly as received (still percent-encoded)."""
    raw = request.scope.get("raw_path")
    return raw.decode("latin-1") if raw else request.url.path


def _upstream_url(path: str, query: str) -> httpx.URL:
    return httpx.URL(path, query=query.encode()) if query else httpx.URL(path)


def _request_headers(request: Request) -> list[tuple[str, str]]:
    return [(k, v) for k, v in request.headers.items() if k.lower() not in REQUEST_EXCLUDED]


def _response_headers(response: httpx.Response, drop: set[str] = frozenset()) -> list[tuple[bytes, bytes]]:
    excluded = HOP_BY_HOP | drop
    return [
        (k.encode("latin-1"), v.encode("latin-1"))
        for k, v in response.headers.multi_items()
        if k.lower() not in excluded
    ]


def create_app(
    settings: Settings | None = None,
    registry: ProviderRegistry | None = None,
    ingestor: Ingestor | None = None,
    upstream_transport: httpx.AsyncBaseTransport | None = None,
    enable_cleanup: bool = True,
    store: ConfigStore | None = None,
) -> FastAPI:
    if settings is None:
        store = store or ConfigStore.default()
        store.initialize()
        settings = store.settings()
    registry = registry if registry is not None else ProviderRegistry.from_settings(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        activity = ActivityLog(store, lambda: app.state.settings.env) if store is not None else None
        app.state.activity_log = activity
        runtime = Runtime(settings, registry, upstream_transport, ingestor)
        app.state.runtime = runtime
        app.state.http = runtime.http
        app.state.ingestor = runtime.ingestor
        app.state.auth_cache = runtime.auth_cache
        app.state.retired = []
        app.state.config_lock = asyncio.Lock()
        runtime.start_cleanup(enable_cleanup)

        async def apply_settings(updated: Settings, updated_registry: ProviderRegistry) -> None:
            old = app.state.runtime
            new = Runtime(updated, updated_registry, upstream_transport)
            if isinstance(old.ingestor, Ingestor):
                new.ingestor.share_download_state(old.ingestor)
            await old.stop_cleanup()
            app.state.runtime = new
            app.state.settings = updated
            app.state.registry = updated_registry
            app.state.http = new.http
            app.state.ingestor = new.ingestor
            app.state.auth_cache = new.auth_cache
            new.start_cleanup(enable_cleanup)
            app.state.retired = [task for task in app.state.retired if not task.done()]
            task = asyncio.create_task(old.close())
            task.add_done_callback(report_retirement)
            app.state.retired.append(task)

        def report_retirement(task: asyncio.Task[None]) -> None:
            if not task.cancelled() and task.exception() is not None:
                error = task.exception()
                logger.error("Retired runtime failed to close: %s", error)

        app.state.apply_settings = apply_settings
        if activity is not None:
            activity.attach()
        try:
            yield
        finally:
            try:
                await app.state.runtime.stop_cleanup()
                await app.state.runtime.close()
                await asyncio.gather(*app.state.retired)
            finally:
                if activity is not None:
                    activity.close()

    app = FastAPI(title="Plex Source Injection Proxy", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.registry = registry
    app.add_middleware(RuntimeMiddleware)
    if store is not None:
        admin_app = FastAPI(title="Plex Source Injection Admin", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
        admin_app.state = app.state
        app.state.admin_app = admin_app
        install_admin(admin_app, store, settings.proxy_port, settings.admin_port)
        admin_app.add_middleware(AdminNetworkMiddleware)
        app.state.proxy_app = app

    async def proxy(request: Request, path: str | None = None, query: str | None = None) -> Response:
        """Stream a request to Plex and stream the response back unchanged."""
        client: httpx.AsyncClient = request.state.runtime.http
        upstream_request = client.build_request(
            request.method,
            _upstream_url(path or _raw_path(request), request.url.query if query is None else query),
            headers=_request_headers(request),
            content=await request.body(),
        )
        try:
            upstream = await client.send(upstream_request, stream=True)
        except httpx.HTTPError as exc:
            logger.warning("Upstream request %s %s failed: %s", request.method, upstream_request.url.path, exc)
            return JSONResponse({"error": "Upstream Plex server unavailable"}, status_code=502)
        response = StreamingResponse(
            upstream.aiter_raw(), status_code=upstream.status_code, background=BackgroundTask(upstream.aclose)
        )
        response.raw_headers = _response_headers(upstream)
        return response

    async def client_authorized(request: Request) -> bool:
        """Check the client's own credentials against Plex before triggering a download."""
        token = request.headers.get("x-plex-token") or request.query_params.get("X-Plex-Token") or ""
        runtime = request.state.runtime
        cache: dict[str, float] = runtime.auth_cache
        cache_key = hashlib.sha256(token.encode()).hexdigest()
        if token and cache.get(cache_key, 0) > time.monotonic():
            return True
        headers = {k.lower(): v for k, v in _request_headers(request)}
        headers["accept"] = "application/json"
        headers.pop("accept-encoding", None)
        if token:
            headers["x-plex-token"] = token
        try:
            response = await runtime.http.get(
                f"/library/sections/{runtime.settings.music_section_id}", headers=headers
            )
        except httpx.HTTPError:
            return False
        if response.status_code == 200:
            if token:
                now = time.monotonic()
                for key in [k for k, expiry in cache.items() if expiry <= now]:
                    del cache[key]
                cache[cache_key] = now + AUTH_CACHE_TTL
            return True
        return False

    async def resolve(request: Request, external_id: str) -> str:
        return await request.state.runtime.ingestor.download_and_register(external_id)

    def owned(request: Request, external_id: str) -> bool:
        if not is_external_id(external_id):
            return False
        try:
            provider, _ = request.state.runtime.registry.resolve(external_id)
        except (KeyError, ValueError):
            return False
        return "music" in provider.enabled_categories

    async def rewrite_query(request: Request) -> tuple[str | None, dict[str, str]]:
        """Replace synthetic ratingKeys embedded in query parameters (e.g. play queue URIs)."""
        raw = request.url.query
        if "ext_" not in raw:
            return None, {}
        params = parse_qsl(raw, keep_blank_values=True)
        found = {
            m.group(2)
            for _, value in params
            for m in EMBEDDED_EXTERNAL_RE.finditer(value)
            if owned(request, m.group(2))
        } | {value for _, value in params if owned(request, value)}
        if not found:
            return None, {}
        mapping = {ext: await resolve(request, ext) for ext in sorted(found)}
        rewritten = []
        for key, value in params:
            if value in mapping:
                value = mapping[value]
            else:
                value = EMBEDDED_EXTERNAL_RE.sub(
                    lambda m: m.group(1) + mapping.get(m.group(2), m.group(2)), value
                )
            rewritten.append((key, value))
        return urlencode(rewritten), mapping

    async def ingest_and_proxy(
        request: Request,
        external_ids: list[str],
        build_path: Callable[[dict[str, str]], str] | None = None,
    ) -> Response:
        try:
            await request.body()
        except ClientDisconnect:
            logger.info("Client disconnected before ingestion request body was received", extra={"activity": "download"})
            return Response(status_code=499)
        if not await client_authorized(request):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        try:
            mapping = {ext: await resolve(request, ext) for ext in external_ids}
            query, query_mapping = await rewrite_query(request)
        except (ItemNotFoundError, KeyError, ValueError) as exc:
            logger.info("External item not found: %s", exc, extra={"activity": "download"})
            return JSONResponse({"error": "External item not found"}, status_code=404)
        except (IngestError, ProviderError) as exc:
            logger.error("Ingest failed: %s", exc, extra={"activity": "download"})
            return JSONResponse({"error": "Failed to fetch external item"}, status_code=502)
        except Exception:
            logger.exception("Unexpected ingest failure", extra={"activity": "download"})
            return JSONResponse({"error": "Failed to fetch external item"}, status_code=502)
        path = build_path(mapping) if build_path is not None else None
        response = await proxy(request, path=path, query=query)
        if response.status_code == 404:
            for ext in {**mapping, **query_mapping}:
                request.state.runtime.ingestor.forget(ext)
        return response

    async def search_handler(request: Request, default_shape: str) -> Response:
        runtime = request.state.runtime
        settings = runtime.settings
        registry = runtime.registry
        client: httpx.AsyncClient = runtime.http
        query = request.query_params.get("query", "")
        upstream_request = client.build_request(
            "GET",
            _upstream_url(_raw_path(request), request.url.query),
            headers=_request_headers(request),
        )

        async def gated_external_search() -> list[dict[str, Any]]:
            if not query.strip() or not len(registry) or not await client_authorized(request):
                return []
            return await search_external(
                registry, query, settings.search_limit, settings.provider_timeout,
                categories=(
                    {"music"} if any(
                        value in {"8", "9", "10", "artist", "album", "track"}
                        for value in request.query_params.get("type", "").split(",")
                    ) else set()
                ) if request.query_params.get("type") else None,
            )

        upstream, external = await asyncio.gather(
            client.send(upstream_request), gated_external_search(), return_exceptions=True
        )
        if isinstance(upstream, BaseException):
            logger.warning("Upstream search %r failed: %s", query, upstream, extra={"activity": "search"})
            return JSONResponse({"error": "Upstream Plex server unavailable"}, status_code=502)
        if isinstance(external, BaseException):
            logger.error("External search %r failed: %s", query, external, extra={"activity": "search"})
            external = []
        drop = {"content-length", "content-encoding"}
        body = upstream.content
        content_type = upstream.headers.get("content-type", "")
        if external and upstream.is_success and "json" in content_type:
            try:
                payload = json.loads(body)
            except ValueError:
                payload = None
            if isinstance(payload, dict):
                body = json.dumps(inject_results(payload, external, default_shape)).encode()
        response = Response(content=body, status_code=upstream.status_code)
        logger.info("Search %r: Plex HTTP %s, %d external result(s)", query, upstream.status_code, len(external), extra={"activity": "search"})
        response.raw_headers = _response_headers(upstream, drop) + [
            (b"content-length", str(len(body)).encode())
        ]
        return response

    @app.get("/hubs/search")
    async def hubs_search(request: Request) -> Response:
        return await search_handler(request, "Hub")

    @app.get("/library/search")
    async def library_search(request: Request) -> Response:
        return await search_handler(request, "SearchResult")

    async def metadata_handler(request: Request, rating_key: str, rest: str = "") -> Response:
        keys = rating_key.split(",")
        external_ids = [k for k in keys if owned(request, k)]
        if not external_ids:
            return await catch_all(request)

        def build_path(mapping: dict[str, str]) -> str:
            path = "/library/metadata/" + ",".join(mapping.get(k, k) for k in keys)
            return f"{path}/{rest}" if rest else path

        return await ingest_and_proxy(request, external_ids, build_path)

    @app.api_route("/library/metadata/{rating_key}", methods=ALL_METHODS)
    async def metadata(request: Request, rating_key: str) -> Response:
        return await metadata_handler(request, rating_key)

    @app.api_route("/library/metadata/{rating_key}/{rest:path}", methods=ALL_METHODS)
    async def metadata_children(request: Request, rating_key: str, rest: str) -> Response:
        return await metadata_handler(request, rating_key, rest)

    @app.api_route("/{full_path:path}", methods=ALL_METHODS)
    async def catch_all(request: Request) -> Response:
        path = request.url.path
        if path.rstrip("/") == "/admin" or path.startswith("/admin/"):
            return JSONResponse({"detail": "Administration is available only on the admin port"}, status_code=404)
        if "ext_" in request.url.query:
            params = parse_qsl(request.url.query, keep_blank_values=True)
            if any(
                owned(request, v) or any(owned(request, m.group(2)) for m in EMBEDDED_EXTERNAL_RE.finditer(v))
                for _, v in params
            ):
                return await ingest_and_proxy(request, [])
        return await proxy(request)

    return app


def run() -> None:  # pragma: no cover - entry point
    import argparse
    import getpass
    from listeners import serve

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    admin_options = parser.add_mutually_exclusive_group()
    admin_options.add_argument("--set-admin-password", action="store_true", help="Set/reset the admin password and exit")
    admin_options.add_argument("--set-admin-networks", metavar="CIDRS", help="Set allowed admin networks and exit; restart to apply")
    args = parser.parse_args()
    store = ConfigStore.default()
    store.initialize()
    if args.set_admin_networks is not None:
        try:
            parse_networks(args.set_admin_networks)
            store.save({**store.values(), "ADMIN_ALLOWED_NETWORKS": args.set_admin_networks})
        except ValueError as exc:
            parser.error(str(exc))
        print("Administrator networks saved. Restart the proxy to apply.")
        return
    if args.set_admin_password:
        password = getpass.getpass("New administrator password (at least 12 characters): ")
        if password != getpass.getpass("Confirm password: "):
            parser.error("Passwords do not match")
        try:
            store.set_password(password)
        except ValueError as exc:
            parser.error(str(exc))
        print("Administrator password saved. Login username: admin")
        return
    settings = store.settings()
    if store.password_hash() is None:
        logger.warning("Admin interface locked: run sudo plex-inject-passwd (RPM) or python main.py --set-admin-password (source checkout)")
    asyncio.run(serve(create_app(settings, store=store), settings.proxy_port, settings.admin_port))


if __name__ == "__main__":  # pragma: no cover
    run()
