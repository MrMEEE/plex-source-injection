import stat
import json

import pytest

from config_store import ConfigStore, DEFAULTS, validate


def test_migration_is_once_and_database_is_private(tmp_path):
    path = tmp_path / "config.sqlite3"
    store = ConfigStore(path)
    assert store.initialize({"PLEX_TOKEN": "legacy", "PROXY_PORT": "8123", "UNRELATED_SECRET": "ignore"})
    assert not store.initialize({"PLEX_TOKEN": "override"})
    assert store.settings().plex_token == "legacy"
    assert store.settings().proxy_port == 8123
    assert "UNRELATED_SECRET" not in store.values()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert ConfigStore(path).settings().plex_token == "legacy"


def test_rpm_dependencies_use_state_bin_not_music(tmp_path, monkeypatch):
    monkeypatch.setattr("config_store.RPM_STATE_DIR", tmp_path)
    store = ConfigStore(tmp_path / "config.sqlite3")
    store.initialize({"DOWNLOAD_DIR": str(tmp_path / "music"), "DEPENDENCY_DIR": str(tmp_path / "music")})
    assert store.settings().get("DEPENDENCY_DIR") == str(tmp_path / "bin")
    with pytest.raises(ValueError, match="RPM managed dependencies"):
        store.save({**store.values(), "DEPENDENCY_DIR": str(tmp_path / "music")})


def test_rpm_migrates_tools_and_manifests_without_moving_music(tmp_path, monkeypatch):
    from dependencies import resolve_tool
    store = ConfigStore(tmp_path / "config.sqlite3")
    previous = tmp_path / "music"
    store.initialize({"DEPENDENCY_DIR": str(previous), "SPOTDL_MODE": "managed"})
    binary = previous / "spotdl/v1/spotdl"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o700)
    (previous / "song.mp3").write_bytes(b"music")
    manifest = {"binary": str(binary), "version": "v1"}
    (previous / "spotdl/current.json").write_text(json.dumps(manifest))
    monkeypatch.setattr("config_store.RPM_STATE_DIR", tmp_path)
    (tmp_path / "bin").mkdir(mode=0o750)
    assert not store.initialize({})
    expected = tmp_path / "bin/spotdl/v1/spotdl"
    assert resolve_tool(store.settings(), "spotdl") == str(expected)
    assert expected.read_text() == binary.read_text()
    assert stat.S_IMODE(expected.stat().st_mode) == 0o700
    assert binary.exists()
    assert not (tmp_path / "bin/song.mp3").exists()
    assert not store.initialize({})
    # A retry after publication but before the SQLite commit uses the same migration.
    with store.connect() as db:
        db.execute("UPDATE configuration SET value=? WHERE key='DEPENDENCY_DIR'", (str(previous),))
    assert not store.initialize({})
    assert resolve_tool(store.settings(), "spotdl") == str(expected)


def test_rpm_dependency_migration_does_not_overwrite_existing_tools(tmp_path, monkeypatch):
    store = ConfigStore(tmp_path / "config.sqlite3")
    previous = tmp_path / "dependencies"
    store.initialize({})
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin/keep").write_text("keep")
    monkeypatch.setattr("config_store.RPM_STATE_DIR", tmp_path)
    with pytest.raises(ValueError, match="not empty"):
        store.initialize({})
    assert store.values()["DEPENDENCY_DIR"] == str(previous)
    assert (tmp_path / "bin/keep").read_text() == "keep"


def test_dotenv_imports_custom_provider_settings(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("SOUNDCLOUD_TOKEN=custom\nPLEX_TOKEN=file\n")
    monkeypatch.setenv("PLEX_TOKEN", "environment")
    monkeypatch.setenv("UNRELATED_SECRET", "private")
    store = ConfigStore(tmp_path / "config.sqlite3")
    store.initialize()
    assert store.settings().get("SOUNDCLOUD_TOKEN") == "custom"
    assert store.settings().plex_token == "environment"
    assert "UNRELATED_SECRET" not in store.values()
    monkeypatch.setenv("PLEX_TOKEN", "changed")
    store.initialize()
    assert store.settings().plex_token == "environment"


def test_save_roundtrip_and_provider_get(tmp_path):
    store = ConfigStore(tmp_path / "config.sqlite3")
    store.initialize({})
    values = {**store.values(), "SOUNDCLOUD_TOKEN": "custom", "DOWNLOAD_DIR": str(tmp_path)}
    store.save(values)
    reopened = ConfigStore(store.path)
    assert reopened.settings().get("SOUNDCLOUD_TOKEN") == "custom"
    assert reopened.settings().plex_download_dir == str(tmp_path)


@pytest.mark.parametrize("key,value", [
    ("PROXY_PORT", "65536"), ("PROXY_PORT", "0"), ("MUSIC_SECTION_ID", "0"),
    ("RETENTION_DAYS", "-1"), ("SEARCH_LIMIT", "0"), ("PROVIDER_TIMEOUT", "nan"),
    ("SCAN_POLL_INTERVAL", "0"), ("SCAN_TIMEOUT", "inf"), ("DOWNLOAD_TIMEOUT", "no"),
    ("CLEANUP_INTERVAL_HOURS", "-1"), ("PLEX_URL", "file:///etc/passwd"),
    ("PLEX_URL", "http://user:pass@localhost"), ("PLEX_URL", "http://localhost:99999"),
    ("DOWNLOAD_DIR", "/"), ("DOWNLOAD_DIR", "relative"), ("PLEX_DOWNLOAD_DIR", "relative"),
    ("AUDIO_FORMAT", "../mp3"), ("SPOTDL_BINARY", ""), ("SPOTDL_PASS_CREDENTIALS", "sometimes"),
    ("ENABLED_PROVIDERS", "invalid-name"), ("INVALID-KEY", "x"), ("CONFIG_DB", "x"),
])
def test_invalid_values_are_not_saved(tmp_path, key, value):
    store = ConfigStore(tmp_path / "config.sqlite3")
    store.initialize({})
    before = store.values()
    with pytest.raises(ValueError):
        store.save({**before, key: value})
    assert store.values() == before


def test_defaults_match_existing_settings():
    settings = validate(DEFAULTS)
    assert settings.provider_timeout == 8
    assert settings.enabled_providers == ("youtube", "spotify")
    assert settings.retention_days == 30
    assert settings.proxy_port == 32399
    assert settings.admin_port == 32300


def test_category_locations_preserve_music_and_resolve_plex_fallbacks(tmp_path):
    from config import Settings
    store = ConfigStore(tmp_path / "config.sqlite3")
    store.initialize({"DOWNLOAD_DIR": str(tmp_path / "legacy-music")})
    settings = store.settings()
    assert settings.download_location("music") == tmp_path / "legacy-music"
    assert settings.plex_download_location("music") == str(tmp_path / "legacy-music")
    updates = {"SERIES_DOWNLOAD_DIR": str(tmp_path / "series"),
               "PLEX_SERIES_DOWNLOAD_DIR": "/plex/shows",
               "MOVIES_DOWNLOAD_DIR": str(tmp_path / "movies"),
               "VIDEOS_DOWNLOAD_DIR": str(tmp_path / "videos")}
    store.save({**store.values(), **updates})
    settings = ConfigStore(store.path).settings()
    assert settings.download_location("series") == tmp_path / "series"
    assert settings.plex_download_location("series") == "/plex/shows"
    assert settings.download_location("movies") == tmp_path / "movies"
    assert settings.plex_download_location("movies") == str(tmp_path / "movies")
    assert settings.download_location("videos") == tmp_path / "videos"
    assert settings.download_location("music") == tmp_path / "legacy-music"
    assert str(Settings().download_location("series")) == "/series/Downloads"
    with pytest.raises(KeyError):
        settings.download_location("unknown")


@pytest.mark.parametrize("key,value", [
    ("ADMIN_PORT", "32399"), ("ADMIN_PORT", "65536"), ("ADMIN_PORT", "0"),
    ("SERIES_DOWNLOAD_DIR", ""), ("SERIES_DOWNLOAD_DIR", "/"),
    ("MOVIES_DOWNLOAD_DIR", "relative"), ("PLEX_VIDEOS_DOWNLOAD_DIR", "relative"),
])
def test_invalid_category_paths_and_admin_ports_not_saved(tmp_path, key, value):
    store = ConfigStore(tmp_path / "config.sqlite3")
    store.initialize({})
    before = store.values()
    with pytest.raises(ValueError):
        store.save({**before, key: value})
    assert store.values() == before


def test_existing_database_receives_new_defaults_without_changing_music(tmp_path):
    store = ConfigStore(tmp_path / "config.sqlite3")
    store.initialize({"DOWNLOAD_DIR": str(tmp_path / "original"), "PROXY_PORT": "8123"})
    with store.connect() as db:
        db.execute("DELETE FROM configuration WHERE key = 'ADMIN_PORT' OR key LIKE '%SERIES_DOWNLOAD_DIR' OR key LIKE '%MOVIES_DOWNLOAD_DIR' OR key LIKE '%VIDEOS_DOWNLOAD_DIR'")
    reopened = ConfigStore(store.path)
    assert not reopened.initialize({"DOWNLOAD_DIR": "/wrong"})
    assert reopened.settings().admin_port == 32300
    assert reopened.settings().proxy_port == 8123
    assert reopened.settings().download_dir == tmp_path / "original"
    assert str(reopened.settings().download_location("movies")) == "/movies/Downloads"


def test_password_is_hashed_and_can_be_reset(tmp_path):
    store = ConfigStore(tmp_path / "config.sqlite3")
    assert not store.verify_password("long-password-123")
    with pytest.raises(ValueError):
        store.set_password("short")
    store.set_password("long-password-123")
    assert "long-password-123" not in store.password_hash()
    assert store.verify_password("long-password-123")
    assert not store.verify_password("wrong-password-123")
    store.set_password("replacement-password")
    assert not store.verify_password("long-password-123")
    assert store.verify_password("replacement-password")


def test_password_setup_entrypoint(tmp_path, monkeypatch, capsys):
    from main import run

    path = tmp_path / "config.sqlite3"
    monkeypatch.setenv("CONFIG_DB", str(path))
    monkeypatch.setattr("sys.argv", ["main.py", "--set-admin-password"])
    monkeypatch.setattr("getpass.getpass", lambda prompt: "setup-password-123")
    run()
    assert ConfigStore(path).verify_password("setup-password-123")
    assert "Login username: admin" in capsys.readouterr().out
