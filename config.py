"""Application configuration loaded from environment variables / ``.env``."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from dotenv import load_dotenv


def _int(env: Mapping[str, str], key: str, default: int) -> int:
    value = env.get(key, "").strip()
    return int(value) if value else default


def _float(env: Mapping[str, str], key: str, default: float) -> float:
    value = env.get(key, "").strip()
    return float(value) if value else default


@dataclass(frozen=True)
class Settings:
    plex_url: str = "http://127.0.0.1:32400"
    plex_token: str = ""
    music_section_id: int = 1
    download_dir: Path = Path("/music/Downloads")
    plex_download_dir: str = "/music/Downloads"
    retention_days: int = 30
    proxy_port: int = 8080
    enabled_providers: tuple[str, ...] = ("youtube", "spotify")
    audio_format: str = "mp3"
    search_limit: int = 10
    provider_timeout: float = 8.0
    download_timeout: float = 300.0
    scan_timeout: float = 120.0
    scan_poll_interval: float = 2.0
    cleanup_interval_hours: float = 24.0
    env: Mapping[str, str] = field(default_factory=dict, repr=False)

    def get(self, key: str, default: str | None = None) -> str | None:
        """Return a raw environment value (used by providers for their own credentials)."""
        value = self.env.get(key)
        return value if value not in (None, "") else default

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        if env is None:
            load_dotenv()
            env = dict(os.environ)
        download_dir = env.get("DOWNLOAD_DIR", "").strip() or "/music/Downloads"
        providers = env.get("ENABLED_PROVIDERS", "youtube,spotify")
        return cls(
            plex_url=(env.get("PLEX_URL", "").strip() or "http://127.0.0.1:32400").rstrip("/"),
            plex_token=env.get("PLEX_TOKEN", "").strip(),
            music_section_id=_int(env, "MUSIC_SECTION_ID", 1),
            download_dir=Path(download_dir),
            plex_download_dir=env.get("PLEX_DOWNLOAD_DIR", "").strip() or download_dir,
            retention_days=_int(env, "RETENTION_DAYS", 30),
            proxy_port=_int(env, "PROXY_PORT", 8080),
            enabled_providers=tuple(
                p.strip().lower() for p in providers.split(",") if p.strip()
            ),
            audio_format=env.get("AUDIO_FORMAT", "").strip() or "mp3",
            search_limit=_int(env, "SEARCH_LIMIT", 10),
            provider_timeout=_float(env, "PROVIDER_TIMEOUT", 8.0),
            download_timeout=_float(env, "DOWNLOAD_TIMEOUT", 300.0),
            scan_timeout=_float(env, "SCAN_TIMEOUT", 120.0),
            scan_poll_interval=_float(env, "SCAN_POLL_INTERVAL", 2.0),
            cleanup_interval_hours=_float(env, "CLEANUP_INTERVAL_HOURS", 24.0),
            env=dict(env),
        )
