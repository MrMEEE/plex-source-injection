import json
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi.testclient import TestClient

from ingest import IngestError, ItemNotFoundError
from main import create_app
from providers import ProviderRegistry
from tests.conftest import FakeProvider, make_provider_class, track

HUBS = {
    "MediaContainer": {
        "size": 1,
        "Hub": [{"type": "track", "hubIdentifier": "track", "size": 1,
                 "Metadata": [{"ratingKey": "100", "type": "track", "title": "Local Song"}]}],
    }
}


class Upstream:
    """Mock Plex Media Server recording every request."""

    def __init__(self, authorized_tokens=("client-token",)):
        self.requests = []
        self.authorized_tokens = set(authorized_tokens)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        token = request.headers.get("X-Plex-Token") or request.url.params.get("X-Plex-Token")
        path = request.url.path
        if token not in self.authorized_tokens:
            return httpx.Response(401, text="Unauthorized")
        if path == "/hubs/search":
            if "json" in request.headers.get("accept", ""):
                return httpx.Response(200, json=HUBS)
            return httpx.Response(200, text="<MediaContainer size=\"0\"/>",
                                  headers={"Content-Type": "text/xml"})
        if path.startswith("/library/sections/"):
            return httpx.Response(200, json={"MediaContainer": {}})
        if path.startswith("/library/metadata/"):
            return httpx.Response(200, json={"MediaContainer": {"Metadata": [{"ratingKey": path.split("/")[3]}]}})
        return httpx.Response(
            201,
            content=b"echo:" + request.content,
            headers={"X-Upstream": "yes", "X-Path": str(request.url.raw_path, "ascii")},
        )

    def paths(self):
        return [r.url.path for r in self.requests]


class FakeIngestor:
    def __init__(self, mapping=None, error=None):
        self.mapping = mapping or {}
        self.error = error
        self.calls = []
        self.forgotten = []

    async def download_and_register(self, external_id):
        self.calls.append(external_id)
        if self.error:
            raise self.error
        return self.mapping[external_id]

    def clear_cache(self):
        pass

    def forget(self, external_id):
        self.forgotten.append(external_id)

    async def trigger_scan(self):
        pass


@pytest.fixture
def upstream():
    return Upstream()


class _AsyncBody(httpx.AsyncByteStream):
    def __init__(self, data):
        self.data = data

    async def __aiter__(self):
        yield self.data


def streaming(handler):
    """Wrap a mock handler so responses are streamed like a real network transport."""

    def wrapped(request):
        response = handler(request)
        return httpx.Response(response.status_code, headers=response.headers, stream=_AsyncBody(response.content))

    return httpx.MockTransport(wrapped)


def build_client(settings, upstream, providers=(), ingestor=None):
    app = create_app(
        settings,
        registry=ProviderRegistry(providers),
        ingestor=ingestor or FakeIngestor(),
        upstream_transport=streaming(upstream),
        enable_cleanup=False,
    )
    return TestClient(app)


JSON_HEADERS = {"Accept": "application/json", "X-Plex-Token": "client-token", "X-Plex-Client-Identifier": "amp"}


def test_hubs_search_injects_external_results(settings, upstream):
    yt = make_provider_class("yt2", "yt", "YouTube")(settings, tracks=[track("dQw4w9WgXcQ", "Never", "Rick")])
    sp = make_provider_class("sp2", "sp", "Spotify")(settings, tracks=[track("4uLU6hMCjMI75M1A2tKUQC")])
    with build_client(settings, upstream, [yt, sp]) as client:
        response = client.get("/hubs/search", params={"query": "never", "limit": "5"}, headers=JSON_HEADERS)

    assert response.status_code == 200
    metadata = response.json()["MediaContainer"]["Hub"][0]["Metadata"]
    assert [m["ratingKey"] for m in metadata] == ["100", "ext_yt_dQw4w9WgXcQ", "ext_sp_4uLU6hMCjMI75M1A2tKUQC"]
    ext = metadata[1]
    assert ext["key"] == "/library/metadata/ext_yt_dQw4w9WgXcQ"
    assert (ext["type"], ext["title"], ext["grandparentTitle"]) == ("track", "Never", "Rick")
    assert yt.searches[0][0] == "never" and sp.searches[0][0] == "never"
    assert int(response.headers["content-length"]) == len(response.content)

    assert sorted(upstream.paths()) == ["/hubs/search", "/library/sections/3"]
    forwarded = next(r for r in upstream.requests if r.url.path == "/hubs/search")
    assert forwarded.url.params["query"] == "never" and forwarded.url.params["limit"] == "5"
    assert forwarded.headers["X-Plex-Client-Identifier"] == "amp"
    assert forwarded.url.host == "plex.test"


def test_library_search_injects_search_results(settings):
    def handler(request):
        return httpx.Response(200, json={"MediaContainer": {"size": 0}})

    app = create_app(settings, registry=ProviderRegistry([FakeProvider(settings, tracks=[track("a")])]),
                     ingestor=FakeIngestor(), upstream_transport=streaming(handler), enable_cleanup=False)
    with TestClient(app) as client:
        response = client.get("/library/search", params={"query": "x"}, headers=JSON_HEADERS)
    results = response.json()["MediaContainer"]["SearchResult"]
    assert results[0]["Metadata"]["ratingKey"] == "ext_fk_a"


def test_search_survives_failing_provider(settings, upstream):
    broken = make_provider_class("broken", "br")(settings, error=RuntimeError("down"))
    slow = make_provider_class("slow", "sl")(settings, tracks=[track("s")], delay=5)
    good = FakeProvider(settings, tracks=[track("ok")])
    with build_client(settings, upstream, [broken, slow, good]) as client:
        response = client.get("/hubs/search", params={"query": "q"}, headers=JSON_HEADERS)
    assert response.status_code == 200
    keys = [m["ratingKey"] for m in response.json()["MediaContainer"]["Hub"][0]["Metadata"]]
    assert keys == ["100", "ext_fk_ok"]


def test_search_does_not_inject_for_unauthorized_client(settings, upstream):
    provider = FakeProvider(settings, tracks=[track("a")])
    with build_client(settings, upstream, [provider]) as client:
        response = client.get("/hubs/search", params={"query": "q"}, headers={"Accept": "application/json"})
    assert response.status_code == 401
    assert "ext_fk" not in response.text
    assert provider.searches == []


def test_search_passes_through_non_json(settings, upstream):
    provider = FakeProvider(settings, tracks=[track("a")])
    with build_client(settings, upstream, [provider]) as client:
        response = client.get("/hubs/search", params={"query": "q", "X-Plex-Token": "client-token"})
    assert response.status_code == 200
    assert response.text == '<MediaContainer size="0"/>'


def test_search_returns_502_when_plex_down(settings):
    def handler(request):
        raise httpx.ConnectError("refused")

    app = create_app(settings, registry=ProviderRegistry([]), ingestor=FakeIngestor(),
                     upstream_transport=streaming(handler), enable_cleanup=False)
    with TestClient(app) as client:
        assert client.get("/hubs/search", params={"query": "q"}).status_code == 502


def test_passthrough_preserves_method_body_headers_and_query(settings, upstream):
    with build_client(settings, upstream) as client:
        response = client.put(
            "/some/path%20with%2Fslash",
            params={"a": "1", "X-Plex-Token": "client-token"},
            content=b"payload",
            headers={"X-Plex-Product": "Plexamp"},
        )
    assert response.status_code == 201
    assert response.content == b"echo:payload"
    assert response.headers["X-Upstream"] == "yes"
    assert response.headers["X-Path"] == "/some/path%20with%2Fslash?a=1&X-Plex-Token=client-token"
    forwarded = upstream.requests[0]
    assert forwarded.method == "PUT"
    assert forwarded.headers["X-Plex-Product"] == "Plexamp"


def test_regular_metadata_is_passed_through(settings, upstream):
    ingestor = FakeIngestor()
    with build_client(settings, upstream, [FakeProvider(settings)], ingestor) as client:
        response = client.get("/library/metadata/123", headers=JSON_HEADERS)
    assert response.json()["MediaContainer"]["Metadata"][0]["ratingKey"] == "123"
    assert ingestor.calls == []
    assert upstream.paths() == ["/library/metadata/123"]


def test_external_metadata_triggers_ingest_and_returns_real_track(settings, upstream):
    ingestor = FakeIngestor({"ext_fk_abc_1": "555"})
    with build_client(settings, upstream, [FakeProvider(settings)], ingestor) as client:
        response = client.get("/library/metadata/ext_fk_abc_1", params={"includeExtras": "1"}, headers=JSON_HEADERS)
        children = client.get("/library/metadata/ext_fk_abc_1/children", headers=JSON_HEADERS)
    assert response.status_code == 200
    assert response.json()["MediaContainer"]["Metadata"][0]["ratingKey"] == "555"
    assert ingestor.calls == ["ext_fk_abc_1", "ext_fk_abc_1"]
    assert "/library/metadata/555" in upstream.paths()
    assert children.status_code == 200
    assert upstream.requests[-1].url.path == "/library/metadata/555/children"
    meta_request = next(r for r in upstream.requests if r.url.path == "/library/metadata/555")
    assert meta_request.url.params["includeExtras"] == "1"


def test_external_metadata_requires_authorized_client(settings, upstream):
    ingestor = FakeIngestor({"ext_fk_abc": "555"})
    with build_client(settings, upstream, [FakeProvider(settings)], ingestor) as client:
        response = client.get("/library/metadata/ext_fk_abc", headers={"X-Plex-Token": "bad"})
    assert response.status_code == 401
    assert ingestor.calls == []


@pytest.mark.parametrize("error,status", [(ItemNotFoundError("x"), 404), (IngestError("x"), 502)])
def test_external_metadata_errors(settings, upstream, error, status):
    ingestor = FakeIngestor(error=error)
    with build_client(settings, upstream, [FakeProvider(settings)], ingestor) as client:
        response = client.get("/library/metadata/ext_fk_abc", headers=JSON_HEADERS)
    assert response.status_code == status


def test_unknown_provider_prefix_is_passed_through(settings, upstream):
    ingestor = FakeIngestor()
    with build_client(settings, upstream, [FakeProvider(settings)], ingestor) as client:
        client.get("/library/metadata/ext_zz_abc", headers=JSON_HEADERS)
    assert ingestor.calls == []
    assert upstream.paths() == ["/library/metadata/ext_zz_abc"]


def test_play_queue_uri_is_rewritten(settings, upstream):
    ingestor = FakeIngestor({"ext_fk_abc": "555"})
    uri = "server://machine/com.plexapp.plugins.library/library/metadata/ext_fk_abc"
    with build_client(settings, upstream, [FakeProvider(settings)], ingestor) as client:
        response = client.post("/playQueues", params={"uri": uri, "type": "audio"}, headers=JSON_HEADERS)
    assert response.status_code == 201
    forwarded = upstream.requests[-1]
    assert forwarded.url.path == "/playQueues"
    params = parse_qs(forwarded.url.query.decode())
    assert params["uri"] == ["server://machine/com.plexapp.plugins.library/library/metadata/555"]
    assert params["type"] == ["audio"]


def test_stale_mapping_is_forgotten_when_plex_returns_404(settings):
    def handler(request):
        if request.url.path.startswith("/library/sections/"):
            return httpx.Response(200, json={})
        return httpx.Response(404)

    ingestor = FakeIngestor({"ext_fk_abc": "555"})
    app = create_app(settings, registry=ProviderRegistry([FakeProvider(settings)]), ingestor=ingestor,
                     upstream_transport=streaming(handler), enable_cleanup=False)
    with TestClient(app) as client:
        assert client.get("/library/metadata/ext_fk_abc", headers=JSON_HEADERS).status_code == 404
    assert ingestor.forgotten == ["ext_fk_abc"]
