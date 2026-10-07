import asyncio
import hashlib
import io
import json
import tarfile
from pathlib import Path

import httpx
import pytest

from config import Settings
from dependencies import DependencyError, DependencyManager, ffmpeg_location, installed, resolve_tool


def release(name, payload, version="v1.2.3", digest=None):
    repository = {"spotdl": "spotDL/spotify-downloader", "yt-dlp": "yt-dlp/yt-dlp", "ffmpeg": "yt-dlp/FFmpeg-Builds"}[name]
    asset = DependencyManager().asset_name(name, version)
    return {
        "tag_name": version, "draft": False, "prerelease": False,
        "assets": [{
            "name": asset, "digest": digest or f"sha256:{hashlib.sha256(payload).hexdigest()}",
            "size": len(payload),
            "browser_download_url": f"https://github.com/{repository}/releases/download/{version}/{asset}",
        }],
    }


@pytest.fixture
def linux(monkeypatch):
    monkeypatch.setattr("dependencies.platform.system", lambda: "Linux")
    monkeypatch.setattr("dependencies.platform.machine", lambda: "x86_64")


def test_verified_version_install_update_and_rollback(tmp_path, linux):
    body = b"#!/bin/sh\necho 1.2.3\n"
    latest = release("spotdl", body)

    def upstream(request):
        if request.url.host == "api.github.com":
            return httpx.Response(200, json=latest)
        return httpx.Response(200, content=body)

    manager = DependencyManager(httpx.MockTransport(upstream))
    settings = Settings.from_env({"DEPENDENCY_DIR": str(tmp_path), "SPOTDL_MODE": "managed"})
    first = asyncio.run(manager.install(settings, "spotdl", "latest"))
    assert Path(first["binary"]).read_bytes() == body
    assert Path(first["binary"]).stat().st_mode & 0o100
    assert resolve_tool(settings, "spotdl") == first["binary"]
    body = b"#!/bin/sh\necho 1.2.4\n"
    latest = release("spotdl", body, "v1.2.4")
    second = asyncio.run(manager.install(settings, "spotdl", "latest"))
    assert installed(settings, "spotdl")["version"] == "v1.2.4"
    assert Path(first["binary"]).is_file()
    assert second["binary"] != first["binary"]
    latest = release("spotdl", body, "v1.2.5", digest="sha256:" + "0" * 64)
    with pytest.raises(DependencyError, match="verification failed"):
        asyncio.run(manager.install(settings, "spotdl", "latest"))
    assert installed(settings, "spotdl") == second
    assert not list((tmp_path / "spotdl").glob("install-*"))


@pytest.mark.parametrize("problem", ["missing_digest", "wrong_host", "wrong_size", "missing_asset", "prerelease"])
def test_reject_untrusted_or_invalid_release(tmp_path, linux, problem):
    body = b"executable"
    data = release("yt-dlp", body)
    if problem == "missing_digest":
        data["assets"][0]["digest"] = None
    elif problem == "wrong_host":
        data["assets"][0]["browser_download_url"] = "https://evil.test/program"
    elif problem == "wrong_size":
        data["assets"][0]["size"] += 1
    elif problem == "missing_asset":
        data["assets"] = []
    else:
        data["prerelease"] = True
    manager = DependencyManager(httpx.MockTransport(
        lambda request: httpx.Response(200, json=data) if request.url.host == "api.github.com" else httpx.Response(200, content=body)
    ))
    settings = Settings.from_env({"DEPENDENCY_DIR": str(tmp_path)})
    with pytest.raises(DependencyError):
        asyncio.run(manager.install(settings, "yt-dlp", "latest"))
    assert installed(settings, "yt-dlp") is None


def test_platform_and_version_guards(tmp_path, monkeypatch):
    manager = DependencyManager()
    monkeypatch.setattr("dependencies.platform.system", lambda: "Linux")
    monkeypatch.setattr("dependencies.platform.machine", lambda: "aarch64")
    assert manager.asset_name("yt-dlp", "2026.08.19") == "yt-dlp_linux_aarch64"
    assert "linuxarm64" in manager.asset_name("ffmpeg", "latest")
    with pytest.raises(DependencyError, match="architecture"):
        manager.asset_name("spotdl", "v4.5.2")
    with pytest.raises(DependencyError, match="Invalid release"):
        asyncio.run(manager.install(Settings(), "spotdl", "../../evil"))


def test_ffmpeg_only_extracts_regular_binaries(tmp_path, linux):
    archive = tmp_path / "build.tar.xz"
    with tarfile.open(archive, "w:xz") as tar:
        for binary in ("ffmpeg", "ffprobe"):
            content = binary.encode()
            member = tarfile.TarInfo(f"build/bin/{binary}")
            member.size = len(content)
            tar.addfile(member, io.BytesIO(content))
        malicious = tarfile.TarInfo("../../outside")
        malicious.size = 3
        tar.addfile(malicious, io.BytesIO(b"bad"))
    DependencyManager.extract_ffmpeg(archive, tmp_path)
    assert (tmp_path / "ffmpeg").read_bytes() == b"ffmpeg"
    assert (tmp_path / "ffprobe").read_bytes() == b"ffprobe"
    assert not (tmp_path.parent / "outside").exists()


def test_ffmpeg_symlinks_are_not_extracted(tmp_path):
    archive = tmp_path / "build.tar.xz"
    with tarfile.open(archive, "w:xz") as tar:
        member = tarfile.TarInfo("build/bin/ffmpeg")
        member.type = tarfile.SYMTYPE
        member.linkname = "/etc/passwd"
        tar.addfile(member)
    with pytest.raises(DependencyError, match="safe ffmpeg"):
        DependencyManager.extract_ffmpeg(archive, tmp_path)


def test_missing_and_external_binary(tmp_path, monkeypatch):
    settings = Settings.from_env({"DEPENDENCY_DIR": str(tmp_path), "SPOTDL_MODE": "managed"})
    with pytest.raises(DependencyError, match="not installed"):
        resolve_tool(settings, "spotdl")
    monkeypatch.setattr("dependencies.shutil.which", lambda name: "/custom/spotdl")
    assert resolve_tool(Settings.from_env({"SPOTDL_BINARY": "custom"}), "spotdl") == "/custom/spotdl"
    root = tmp_path / "spotdl"
    root.mkdir()
    (root / "current.json").write_text(json.dumps({"binary": "/etc/passwd"}))
    with pytest.raises(DependencyError, match="manifest"):
        installed(settings, "spotdl")


def test_failed_executable_probe_preserves_selection(tmp_path, linux):
    body = b"#!/bin/sh\necho version\n"

    def upstream(request):
        if request.url.host == "api.github.com":
            return httpx.Response(200, json=release("spotdl", body))
        return httpx.Response(200, content=body)

    manager = DependencyManager(httpx.MockTransport(upstream))
    settings = Settings.from_env({"DEPENDENCY_DIR": str(tmp_path)})
    previous = asyncio.run(manager.install(settings, "spotdl", "latest"))
    body = b"#!/bin/sh\nexit 1\n"
    with pytest.raises(DependencyError, match="version check failed"):
        asyncio.run(manager.install(settings, "spotdl", "latest"))
    assert installed(settings, "spotdl") == previous


def test_external_ffmpeg_retains_exact_executable_name(monkeypatch):
    monkeypatch.setattr("dependencies.shutil.which", lambda name: "/custom/ffmpeg-custom")
    settings = Settings.from_env({"FFMPEG_BINARY": "/custom/ffmpeg-custom"})
    assert ffmpeg_location(settings) == "/custom/ffmpeg-custom"
    assert ffmpeg_location(Settings()) is None
