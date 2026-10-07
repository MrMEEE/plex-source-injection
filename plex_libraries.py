"""Music-library discovery and validation for the audio ingestion pipeline."""

from __future__ import annotations

from typing import Any, TypedDict

from plexapi.exceptions import BadRequest, NotFound, Unauthorized
from plexapi.server import PlexServer
from requests import RequestException, Session

from config import Settings


class PlexLibraryError(Exception):
    """The configured Plex connection or library cannot be used for music."""


class MusicLibrary(TypedDict):
    id: str
    title: str


def validate_music_section(section: Any, section_id: int) -> None:
    if section.type != "artist":
        raise PlexLibraryError(
            f"MUSIC_SECTION_ID={section_id} points to {section.title!r} "
            f"(type {section.type!r}), not a Music library. "
            "Open General, load Plex music libraries, select a Music library and save. "
            "The current plugins download audio tracks, not movies or series."
        )


def music_libraries(settings: Settings) -> list[MusicLibrary]:
    if not settings.plex_token:
        raise PlexLibraryError("Save your Plex server URL and token before loading music libraries.")
    try:
        with Session() as session:
            server = PlexServer(settings.plex_url, settings.plex_token, timeout=10, session=session)
            return [
                {"id": str(section.key), "title": section.title}
                for section in server.library.sections() if section.type == "artist"
            ]
    except Unauthorized as exc:
        raise PlexLibraryError("Plex rejected the saved token. Authorize Plex again or save a valid server-owner token.") from exc
    except (BadRequest, NotFound, RequestException) as exc:
        raise PlexLibraryError("Could not load Plex libraries. Check the saved Plex URL, token and server connectivity.") from exc
