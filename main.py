"""FastAPI reverse proxy sitting between Plex clients and Plex Media Server."""

from __future__ import annotations

import asyncio
import contextlib
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

from cleanup import run_cleanup_loop
from config import Settings
from ingest import IngestError, Ingestor, ItemNotFoundError
from providers import ProviderError, ProviderRegistry, is_external_id
from search import inject_results, search_external

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
) -> FastAPI:
    settings = settings or Settings.from_env()
    registry = registry if registry is not None else ProviderRegistry.from_settings(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        client = httpx.AsyncClient(
            base_url=settings.plex_url,
            timeout=httpx.Timeout(None, connect=10.0),
            transport=upstream_transport,
            follow_redirects=False,
        )
        app.state.http = client
        app.state.ingestor = ingestor or Ingestor(settings, registry, http_client=client)
        app.state.auth_cache = {}
        cleanup_task = None
        if enable_cleanup and settings.retention_days > 0:

            async def _on_deleted(_paths: list[Any]) -> None:
                app.state.ingestor.clear_cache()
                with contextlib.suppress(Exception):
                    await app.state.ingestor.trigger_scan()

            cleanup_task = asyncio.create_task(
                run_cleanup_loop(
                    settings.download_dir,
                    settings.retention_days,
                    settings.cleanup_interval_hours * 3600,
                    on_deleted=_on_deleted,
                )
            )
        try:
            yield
        finally:
            if cleanup_task is not None:
                cleanup_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await cleanup_task
            await client.aclose()

    app = FastAPI(title="Plex Source Injection Proxy", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.registry = registry

    async def proxy(request: Request, path: str | None = None, query: str | None = None) -> Response:
        """Stream a request to Plex and stream the response back unchanged."""
        client: httpx.AsyncClient = request.app.state.http
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
        cache: dict[str, float] = request.app.state.auth_cache
        cache_key = hashlib.sha256(token.encode()).hexdigest()
        if token and cache.get(cache_key, 0) > time.monotonic():
            return True
        headers = {k.lower(): v for k, v in _request_headers(request)}
        headers["accept"] = "application/json"
        headers.pop("accept-encoding", None)
        if token:
            headers["x-plex-token"] = token
        try:
            response = await request.app.state.http.get(
                f"/library/sections/{settings.music_section_id}", headers=headers
            )
        except httpx.HTTPError:
            return False
        if response.status_code == 200:
            if token:
                cache[cache_key] = time.monotonic() + AUTH_CACHE_TTL
            return True
        return False

    async def resolve(request: Request, external_id: str) -> str:
        return await request.app.state.ingestor.download_and_register(external_id)

    def owned(external_id: str) -> bool:
        if not is_external_id(external_id):
            return False
        try:
            registry.resolve(external_id)
        except (KeyError, ValueError):
            return False
        return True

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
            if owned(m.group(2))
        } | {value for _, value in params if owned(value)}
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
        if not await client_authorized(request):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        try:
            mapping = {ext: await resolve(request, ext) for ext in external_ids}
            query, query_mapping = await rewrite_query(request)
        except (ItemNotFoundError, KeyError, ValueError) as exc:
            logger.info("External item not found: %s", exc)
            return JSONResponse({"error": "External item not found"}, status_code=404)
        except (IngestError, ProviderError) as exc:
            logger.error("Ingest failed: %s", exc)
            return JSONResponse({"error": "Failed to fetch external item"}, status_code=502)
        except Exception:
            logger.exception("Unexpected ingest failure")
            return JSONResponse({"error": "Failed to fetch external item"}, status_code=502)
        path = build_path(mapping) if build_path is not None else None
        response = await proxy(request, path=path, query=query)
        if response.status_code == 404:
            for ext in {**mapping, **query_mapping}:
                request.app.state.ingestor.forget(ext)
        return response

    async def search_handler(request: Request, default_shape: str) -> Response:
        client: httpx.AsyncClient = request.app.state.http
        query = request.query_params.get("query", "")
        upstream_request = client.build_request(
            "GET",
            _upstream_url(_raw_path(request), request.url.query),
            headers=_request_headers(request),
        )
        if query.strip() and len(registry) and await client_authorized(request):
            external_search = search_external(
                registry, query, settings.search_limit, settings.provider_timeout
            )
        else:
            external_search = asyncio.sleep(0, result=[])
        upstream, external = await asyncio.gather(
            client.send(upstream_request), external_search, return_exceptions=True
        )
        if isinstance(upstream, BaseException):
            logger.warning("Upstream search failed: %s", upstream)
            return JSONResponse({"error": "Upstream Plex server unavailable"}, status_code=502)
        if isinstance(external, BaseException):
            logger.error("External search failed: %s", external)
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
        external_ids = [k for k in keys if owned(k)]
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
        if "ext_" in request.url.query:
            params = parse_qsl(request.url.query, keep_blank_values=True)
            if any(
                owned(v) or any(owned(m.group(2)) for m in EMBEDDED_EXTERNAL_RE.finditer(v))
                for _, v in params
            ):
                return await ingest_and_proxy(request, [])
        return await proxy(request)

    return app


def run() -> None:  # pragma: no cover - entry point
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings.from_env()
    uvicorn.run(
        create_app(settings),
        host="0.0.0.0",
        port=settings.proxy_port,
        proxy_headers=False,
        # Plex already sends these; avoid duplicating them on proxied responses.
        server_header=False,
        date_header=False,
    )


if __name__ == "__main__":  # pragma: no cover
    run()
