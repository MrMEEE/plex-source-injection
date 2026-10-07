"""Persistent configuration, validation and administrator credentials."""

from __future__ import annotations

import hashlib
import hmac
import math
import os
import re
import secrets
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Mapping
from urllib.parse import urlsplit

from dotenv import dotenv_values

from config import CATEGORY_PATH_KEYS, Settings
from admin_access import DEFAULT_ADMIN_NETWORKS, parse_networks

DEFAULTS = {
    "PLEX_URL": "http://127.0.0.1:32400",
    "PLEX_TOKEN": "",
    "MUSIC_SECTION_ID": "1",
    "DOWNLOAD_DIR": "/music/Downloads",
    "PLEX_DOWNLOAD_DIR": "",
    "RETENTION_DAYS": "30",
    "PROXY_PORT": "32399",
    "ADMIN_PORT": "32300",
    "ENABLED_PROVIDERS": "youtube,spotify",
    "SPOTIFY_CLIENT_ID": "",
    "SPOTIFY_CLIENT_SECRET": "",
    "YOUTUBE_API_KEY": "",
    "AUDIO_FORMAT": "mp3",
    "SEARCH_LIMIT": "10",
    "PROVIDER_TIMEOUT": "8",
    "DOWNLOAD_TIMEOUT": "300",
    "SCAN_TIMEOUT": "120",
    "SCAN_POLL_INTERVAL": "2",
    "CLEANUP_INTERVAL_HOURS": "24",
    "SPOTDL_BINARY": "spotdl",
    "SPOTDL_PASS_CREDENTIALS": "false",
    "SPOTIFY_CATEGORIES": "music",
    "YOUTUBE_CATEGORIES": "music",
    "SPOTDL_MODE": "external",
    "YTDLP_MODE": "bundled",
    "YTDLP_BINARY": "yt-dlp",
    "FFMPEG_MODE": "external",
    "FFMPEG_BINARY": "ffmpeg",
    "DEPENDENCY_DIR": str(Path("dependencies").resolve()),
    "ADMIN_ALLOWED_NETWORKS": DEFAULT_ADMIN_NETWORKS,
}
for _local_key, _plex_key, _default in CATEGORY_PATH_KEYS.values():
    DEFAULTS.setdefault(_local_key, _default)
    DEFAULTS.setdefault(_plex_key, "")
SECRET_KEYS = {"PLEX_TOKEN", "SPOTIFY_CLIENT_SECRET", "YOUTUBE_API_KEY"}
KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,79}$")
RESERVED_KEYS = {"CONFIG_DB", "ADMIN_PASSWORD"}


def is_secret(key: str) -> bool:
    return key in SECRET_KEYS or any(word in key for word in ("TOKEN", "SECRET", "PASSWORD", "KEY"))


def validate_key(key: str) -> None:
    if not KEY_RE.fullmatch(key) or key in RESERVED_KEYS:
        raise ValueError(f"Invalid configuration key: {key}")


def validate(values: Mapping[str, str]) -> Settings:
    parse_networks(values.get("ADMIN_ALLOWED_NETWORKS", DEFAULT_ADMIN_NETWORKS))
    for key, value in values.items():
        validate_key(key)
        if len(value) > 8192 or "\x00" in value:
            raise ValueError(f"{key}: value is too long or contains a null byte")
    for key, minimum, maximum in (
        ("MUSIC_SECTION_ID", 1, None), ("RETENTION_DAYS", 0, None),
        ("PROXY_PORT", 1, 65535), ("ADMIN_PORT", 1, 65535), ("SEARCH_LIMIT", 1, None),
    ):
        try:
            value = int(values[key])
        except ValueError:
            raise ValueError(f"{key} must be an integer") from None
        if value < minimum or (maximum is not None and value > maximum):
            raise ValueError(f"{key} is out of range")
    if values["ADMIN_PORT"] == values["PROXY_PORT"] or int(values["ADMIN_PORT"]) == int(values["PROXY_PORT"]):
        raise ValueError("ADMIN_PORT and PROXY_PORT must be different")
    for key in (
        "PROVIDER_TIMEOUT", "DOWNLOAD_TIMEOUT", "SCAN_TIMEOUT",
        "SCAN_POLL_INTERVAL", "CLEANUP_INTERVAL_HOURS",
    ):
        try:
            value = float(values[key])
        except ValueError:
            raise ValueError(f"{key} must be a number") from None
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"{key} must be finite and greater than zero")
    url = urlsplit(values["PLEX_URL"])
    try:
        port = url.port
    except ValueError:
        raise ValueError("PLEX_URL has an invalid port") from None
    if (
        url.scheme not in ("http", "https") or not url.hostname
        or url.username or url.password or url.query or url.fragment
        or (port is not None and port < 1)
    ):
        raise ValueError("PLEX_URL must be an HTTP(S) URL without credentials, query or fragment")
    for local_key, plex_key, _ in CATEGORY_PATH_KEYS.values():
        for key in (local_key, plex_key):
            value = values.get(key, DEFAULTS[key])
            if key == plex_key and not value:
                continue
            if not Path(value).is_absolute() or Path(value) == Path("/"):
                raise ValueError(f"{key} must be an absolute directory other than /")
    if not re.fullmatch(r"[a-zA-Z0-9]+", values["AUDIO_FORMAT"]):
        raise ValueError("AUDIO_FORMAT must be an alphanumeric format name")
    if not values["SPOTDL_BINARY"].strip():
        raise ValueError("SPOTDL_BINARY must not be empty")
    if values["SPOTDL_PASS_CREDENTIALS"].lower() not in ("true", "false", "1", "0", "yes", "no"):
        raise ValueError("SPOTDL_PASS_CREDENTIALS must be true or false")
    for key, modes in (
        ("SPOTDL_MODE", ("external", "managed")),
        ("YTDLP_MODE", ("bundled", "external", "managed")),
        ("FFMPEG_MODE", ("external", "managed")),
    ):
        if values.get(key, DEFAULTS[key]) not in modes:
            raise ValueError(f"{key} must be one of {', '.join(modes)}")
    if not Path(values.get("DEPENDENCY_DIR", DEFAULTS["DEPENDENCY_DIR"])).is_absolute():
        raise ValueError("DEPENDENCY_DIR must be an absolute directory")
    names = [name.strip() for name in values["ENABLED_PROVIDERS"].split(",") if name.strip()]
    if any(not re.fullmatch(r"[a-z][a-z0-9_]*", name) for name in names):
        raise ValueError("ENABLED_PROVIDERS must contain comma-separated provider names")
    return Settings.from_env(values)


class ConfigStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        # Create with restrictive permissions before SQLite writes any credentials.
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        path.chmod(0o600)
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS configuration (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")

    @classmethod
    def default(cls) -> "ConfigStore":
        return cls(Path(os.environ.get("CONFIG_DB", "config.sqlite3")).expanduser())

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def initialize(self, env: Mapping[str, str] | None = None) -> bool:
        """Import known legacy settings exactly once; never copy unrelated environment secrets."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM metadata WHERE key = 'initialized'").fetchone():
                return False
            file_values = {} if env is not None else {
                k: v for k, v in dotenv_values(".env").items() if v is not None
            }
            source = dict(env) if env is not None else {**file_values, **os.environ}
            values = dict(DEFAULTS)
            values["DEPENDENCY_DIR"] = str((self.path.resolve().parent / "dependencies"))
            for key in DEFAULTS:
                if source.get(key, "").strip():
                    values[key] = source[key].strip()
            values["ENABLED_PROVIDERS"] = source.get("ENABLED_PROVIDERS", values["ENABLED_PROVIDERS"]).lower()
            for key in file_values:
                if key not in DEFAULTS and key not in RESERVED_KEYS:
                    values[key] = source[key]
            validate(values)
            db.executemany("INSERT INTO configuration VALUES (?, ?)", values.items())
            db.execute("INSERT INTO metadata VALUES ('initialized', '1')")
            return True

    def values(self) -> dict[str, str]:
        with self.connect() as db:
            return {
                **DEFAULTS,
                "DEPENDENCY_DIR": str(self.path.resolve().parent / "dependencies"),
                **dict(db.execute("SELECT key, value FROM configuration")),
            }

    def settings(self) -> Settings:
        return validate(self.values())

    def save(self, values: Mapping[str, str]) -> None:
        validate(values)
        with self.connect() as db:
            db.executemany(
                "INSERT INTO configuration VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                values.items(),
            )

    def password_hash(self) -> str | None:
        with self.connect() as db:
            row = db.execute("SELECT value FROM metadata WHERE key='admin_password'").fetchone()
        return row[0] if row else None

    def set_password(self, password: str) -> None:
        if len(password) < 12 or len(password) > 1024:
            raise ValueError("Administrator password must be 12 to 1024 characters")
        salt = secrets.token_hex(16)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 600_000).hex()
        with self.connect() as db:
            db.execute(
                "INSERT INTO metadata VALUES ('admin_password', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (f"{salt}:{digest}",),
            )

    def verify_password(self, password: str) -> bool:
        saved = self.password_hash()
        if saved is None or len(password) > 1024:
            return False
        salt, expected = saved.split(":")
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 600_000).hex()
        return hmac.compare_digest(digest, expected)
