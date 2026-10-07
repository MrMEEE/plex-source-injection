import asyncio

from providers import ProviderRegistry
from search import inject_results, search_external, to_plex_track
from tests.conftest import FakeProvider, make_provider_class, track


def test_to_plex_track_schema(settings):
    provider = make_provider_class("youtube2", "yt", "YouTube")(settings)
    item = to_plex_track(
        provider, track("dQw4w9WgXcQ", title="Never", artist="Rick", album="Chan", duration_ms=1000, thumb="t")
    )
    assert item["type"] == "track"
    assert item["title"] == "Never"
    assert item["grandparentTitle"] == "Rick"
    assert item["parentTitle"] == "Chan"
    assert item["ratingKey"] == "ext_yt_dQw4w9WgXcQ"
    assert item["key"] == "/library/metadata/ext_yt_dQw4w9WgXcQ"
    assert item["duration"] == 1000 and item["thumb"] == "t"


def test_to_plex_track_album_falls_back_to_provider(settings):
    item = to_plex_track(FakeProvider(settings), track("x"))
    assert item["parentTitle"] == "Fake"


def test_search_external_isolates_failing_and_slow_providers(settings):
    good = FakeProvider(settings, tracks=[track("a"), track("b")])
    broken = make_provider_class("broken", "br")(settings, error=RuntimeError("down"))
    slow = make_provider_class("slow", "sl")(settings, tracks=[track("c")], delay=5)
    registry = ProviderRegistry([good, broken, slow])

    items = asyncio.run(search_external(registry, "  hello ", limit=10, timeout=0.1))

    assert [i["ratingKey"] for i in items] == ["ext_fk_a", "ext_fk_b"]
    assert good.searches == [("hello", 10)]
    assert broken.searches and slow.searches


def test_search_external_runs_concurrently(settings):
    providers = [make_provider_class(f"p{i}", f"p{i}")(settings, tracks=[track("x")], delay=0.1) for i in range(5)]
    loop = asyncio.new_event_loop()
    try:
        start = loop.time()
        items = loop.run_until_complete(search_external(ProviderRegistry(providers), "q", 5, 1.0))
        elapsed = loop.time() - start
    finally:
        loop.close()
    assert len(items) == 5
    assert elapsed < 0.4


def test_search_external_skips_empty_query_and_invalid_ids(settings):
    provider = FakeProvider(settings, tracks=[track("ok"), track("bad id")])
    registry = ProviderRegistry([provider])
    assert asyncio.run(search_external(registry, "   ", 10, 1)) == []
    assert provider.searches == []
    items = asyncio.run(search_external(registry, "q", 10, 1))
    assert [i["ratingKey"] for i in items] == ["ext_fk_ok"]


ITEMS = [{"ratingKey": "ext_fk_a", "type": "track"}, {"ratingKey": "ext_fk_b", "type": "track"}]


def test_inject_into_existing_track_hub():
    payload = {"MediaContainer": {"size": 2, "Hub": [
        {"type": "artist", "size": 1, "Metadata": [{"ratingKey": "1"}]},
        {"type": "track", "size": 1, "Metadata": [{"ratingKey": "2"}]},
    ]}}
    inject_results(payload, list(ITEMS))
    hub = payload["MediaContainer"]["Hub"][1]
    assert [m["ratingKey"] for m in hub["Metadata"]] == ["2", "ext_fk_a", "ext_fk_b"]
    assert hub["size"] == 3
    assert payload["MediaContainer"]["Hub"][0]["size"] == 1


def test_inject_creates_track_hub_when_missing():
    payload = {"MediaContainer": {"size": 1, "Hub": [{"type": "album", "size": 0}]}}
    inject_results(payload, list(ITEMS))
    hubs = payload["MediaContainer"]["Hub"]
    assert hubs[1]["type"] == "track" and hubs[1]["size"] == 2
    assert payload["MediaContainer"]["size"] == 2


def test_inject_into_search_results():
    payload = {"MediaContainer": {"size": 1, "SearchResult": [{"score": 0.9, "Metadata": {"ratingKey": "1"}}]}}
    inject_results(payload, list(ITEMS))
    results = payload["MediaContainer"]["SearchResult"]
    assert [r["Metadata"]["ratingKey"] for r in results] == ["1", "ext_fk_a", "ext_fk_b"]
    assert payload["MediaContainer"]["size"] == 3


def test_inject_into_flat_metadata_and_empty_containers():
    payload = {"MediaContainer": {"size": 0, "Metadata": []}}
    inject_results(payload, list(ITEMS))
    assert payload["MediaContainer"]["size"] == 2

    empty = inject_results({"MediaContainer": {"size": 0}}, list(ITEMS), default_shape="SearchResult")
    assert len(empty["MediaContainer"]["SearchResult"]) == 2

    unchanged = {"MediaContainer": {"size": 0}}
    assert inject_results(unchanged, []) == {"MediaContainer": {"size": 0}}
def test_category_permissions_filter_music_results(settings):
    import asyncio
    from dataclasses import replace

    from providers import ProviderRegistry
    from search import search_external
    from tests.conftest import FakeProvider, track

    provider = FakeProvider(replace(settings, env={"FAKE_CATEGORIES": ""}), tracks=[track("abc")])
    registry = ProviderRegistry([provider])
    assert asyncio.run(search_external(registry, "song", 10, 1)) == []
    assert provider.searches == []
    provider.settings = replace(settings, env={"FAKE_CATEGORIES": "music"})
    assert asyncio.run(search_external(registry, "song", 10, 1, categories={"videos"})) == []
    assert provider.searches == []
    assert len(asyncio.run(search_external(registry, "song", 10, 1, categories={"music"}))) == 1
