"""Abstract provider interface shared by every external source."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from config import Settings

EXTERNAL_PREFIX = "ext"
PROVIDER_PREFIX_RE = re.compile(r"^[a-z0-9]+$")
ITEM_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
EXTERNAL_ID_RE = re.compile(rf"^{EXTERNAL_PREFIX}_([a-z0-9]+)_([A-Za-z0-9_-]+)$")

# Extensions of partially written files produced by downloaders.
_TEMP_SUFFIXES = {".part", ".ytdl", ".temp", ".tmp"}


class ProviderError(Exception):
    """Raised when a provider fails to search or download."""


class ProviderConfigurationError(ProviderError):
    """Raised when a provider cannot be initialised (e.g. missing credentials)."""


@dataclass(frozen=True)
class ExternalTrack:
    """Normalised track metadata returned by every provider."""

    provider: str
    item_id: str
    title: str
    artist: str
    album: str | None = None
    duration_ms: int | None = None
    thumb: str | None = None
    url: str | None = None


def make_external_id(prefix: str, item_id: str) -> str:
    """Build the synthetic ratingKey ``ext_<prefix>_<item_id>``."""
    if not PROVIDER_PREFIX_RE.match(prefix):
        raise ValueError(f"Invalid provider prefix: {prefix!r}")
    if not ITEM_ID_RE.match(item_id):
        raise ValueError(f"Invalid item id: {item_id!r}")
    return f"{EXTERNAL_PREFIX}_{prefix}_{item_id}"


def parse_external_id(external_id: str) -> tuple[str, str]:
    """Split ``ext_<prefix>_<item_id>`` into ``(prefix, item_id)``.

    Item IDs may themselves contain underscores (e.g. YouTube video IDs), so only
    the first two separators are significant.
    """
    match = EXTERNAL_ID_RE.match(external_id)
    if not match:
        raise ValueError(f"Not an external ratingKey: {external_id!r}")
    return match.group(1), match.group(2)


def is_external_id(value: str) -> bool:
    return bool(EXTERNAL_ID_RE.match(value))


def find_downloaded_file(output_dir: Path, item_id: str) -> Path | None:
    """Return the newest completed file in ``output_dir`` tagged with ``[item_id]``."""
    if not output_dir.is_dir():
        return None
    marker = f"[{item_id}]"
    candidates = [
        p
        for p in output_dir.iterdir()
        if p.is_file() and p.stem.endswith(marker) and p.suffix.lower() not in _TEMP_SUFFIXES
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


class BaseProvider(ABC):
    """Interface every external source must implement.

    Subclasses must define:

    * ``name`` – identifier used in ``ENABLED_PROVIDERS`` (e.g. ``"youtube"``)
    * ``prefix`` – lowercase alphanumeric namespace used in synthetic ratingKeys
      (e.g. ``"yt"`` -> ``ext_yt_<id>``)
    * ``display_name`` – human readable source name shown in clients

    Downloaded files must be written to ``output_dir`` with ``[<item_id>]`` right
    before the extension so they can be matched after the Plex scan.

    Raise :class:`ProviderConfigurationError` from ``__init__`` when the provider
    cannot run (e.g. missing credentials); the registry will skip it.
    """

    name: ClassVar[str]
    prefix: ClassVar[str]
    display_name: ClassVar[str]

    def __init__(self, settings: "Settings") -> None:
        self.settings = settings

    def external_id(self, item_id: str) -> str:
        return make_external_id(self.prefix, item_id)

    @abstractmethod
    async def search(self, query: str, limit: int) -> list[ExternalTrack]:
        """Return up to ``limit`` tracks matching ``query``."""

    @abstractmethod
    async def fetch_metadata(self, item_id: str) -> ExternalTrack | None:
        """Return metadata for a single item, or ``None`` if it does not exist."""

    @abstractmethod
    async def download(self, item_id: str, output_dir: Path) -> Path:
        """Download ``item_id`` into ``output_dir`` and return the resulting file path."""
