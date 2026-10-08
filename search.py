"""Search aggregation: fan out to external providers and inject results into Plex responses."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from providers import BaseProvider, ExternalTrack, ProviderRegistry

logger = logging.getLogger(__name__)


def to_plex_track(provider: BaseProvider, track: ExternalTrack) -> dict[str, Any]:
    """Convert an :class:`ExternalTrack` into a Plex ``Track`` metadata object."""
    rating_key = provider.external_id(track.item_id)
    item: dict[str, Any] = {
        "ratingKey": rating_key,
        "key": f"/library/metadata/{rating_key}",
        "guid": f"ext://{provider.name}/{track.item_id}",
        "type": "track",
        "title": f"[{provider.display_name}] {track.title}",
        "grandparentTitle": track.artist,
        "parentTitle": track.album or provider.display_name,
        "sourceTitle": provider.display_name,
    }
    if track.duration_ms:
        item["duration"] = int(track.duration_ms)
    if track.thumb:
        item["thumb"] = track.thumb
        item["parentThumb"] = track.thumb
    return item


async def _search_provider(
    provider: BaseProvider, query: str, limit: int, timeout: float
) -> list[dict[str, Any]]:
    logger.info("Searching %s for %r (limit %d)", provider.name, query, limit)
    try:
        tracks = await asyncio.wait_for(provider.search(query, limit), timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning("Provider %s search %r timed out after %.1fs", provider.name, query, timeout)
        return []
    except Exception:
        logger.exception("Provider %s search %r failed", provider.name, query)
        return []
    items = []
    for track in tracks[:limit]:
        try:
            items.append(to_plex_track(provider, track))
        except ValueError:
            logger.warning("Provider %s returned invalid item id %r", provider.name, track.item_id)
    logger.info("Search %s for %r returned %d result(s): %s", provider.name, query, len(items),
                "; ".join(f"{item['title']} [{item['ratingKey']}]" for item in items))
    return items


async def search_external(
    registry: ProviderRegistry, query: str, limit: int, timeout: float,
    categories: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Query every enabled provider concurrently; failures yield no items for that provider."""
    query = query.strip()
    if not query or not len(registry):
        return []
    results = await asyncio.gather(
        *(
            _search_provider(p, query, limit, timeout) for p in registry.providers
            if "music" in p.enabled_categories and (categories is None or "music" in categories)
        )
    )
    return [item for items in results for item in items]


def inject_results(
    payload: dict[str, Any], items: list[dict[str, Any]], default_shape: str = "Hub"
) -> dict[str, Any]:
    """Append external track items to a Plex JSON search response (in place).

    Supports the three response shapes used by Plex search endpoints:

    * ``/hubs/search`` – ``MediaContainer.Hub[]`` (items go into the ``track`` hub)
    * ``/library/search`` – ``MediaContainer.SearchResult[]``
    * flat ``MediaContainer.Metadata[]``

    ``default_shape`` (``"Hub"``, ``"SearchResult"`` or ``"Metadata"``) is used when
    the upstream response contains no results at all.
    """
    if not items:
        return payload
    container = payload.setdefault("MediaContainer", {})
    shape = next((k for k in ("Hub", "SearchResult", "Metadata") if k in container), default_shape)

    if shape == "Hub":
        hubs = container.setdefault("Hub", [])
        hub = next((h for h in hubs if h.get("type") == "track"), None)
        if hub is None:
            hub = {
                "hubIdentifier": "track",
                "title": "Tracks",
                "type": "track",
                "size": 0,
                "more": False,
                "Metadata": [],
            }
            hubs.append(hub)
            container["size"] = len(hubs)
        metadata = hub.setdefault("Metadata", [])
        metadata.extend(items)
        hub["size"] = len(metadata)
    elif shape == "SearchResult":
        results = container.setdefault("SearchResult", [])
        results.extend({"score": 0.5, "Metadata": item} for item in items)
        container["size"] = len(results)
    else:
        metadata = container.setdefault("Metadata", [])
        metadata.extend(items)
        container["size"] = len(metadata)
    return payload
