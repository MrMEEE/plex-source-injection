import asyncio
import hashlib
import io
import json
import tarfile
import zipfile
from pathlib import Path

import httpx
import pytest

from config import Settings
from dependencies import (
    DependencyError, DependencyManager, deno_location, ffmpeg_location, installed, resolve_tool, tool_environment,
)


def release(name, payload, version="v1.2.3", digest=None):
    repository = {"spotdl": "spotDL/spotify-downloader", "yt-dlp": "yt-dlp/yt-dlp", "ffmpeg": "yt-dlp/FFmpeg-Builds", "deno": "denoland/deno"}[name]
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


def test_probe_reports_glibc_failure_and_keeps_output_bounded(tmp_path):
    binary = tmp_path / "spotdl"
    binary.write_text(
        "#!/bin/sh\n"
        "echo \"Failed to load Python shared library: version GLIBC_2.38 not found\" >&2\n"
        "exit 255\n"
    )
    binary.chmod(0o700)
    with pytest.raises(DependencyError, match="GLIBC_2.38") as error:
        asyncio.run(DependencyManager.probe(binary, "spotdl"))
    assert "exit 255" in str(error.value)
    assert "externally installed executable" in str(error.value)
    assert "do not replace the system glibc" in str(error.value)
    binary.write_text("#!/bin/sh\nhead -c 4000 /dev/zero | tr '\\000' x\nexit 1\n")
    with pytest.raises(DependencyError) as error:
        asyncio.run(DependencyManager.probe(binary, "spotdl"))
    assert len(str(error.value)) < 2200


def test_python_spotdl_install_selection_and_failed_update(tmp_path, monkeypatch):
    version = "4.5.2"
    data = {
        "info": {"version": version},
        "urls": [{"filename": f"spotdl-{version}-py3-none-any.whl", "yanked": False,
                  "digests": {"sha256": "a" * 64},
                  "url": f"https://files.pythonhosted.org/packages/test/spotdl-{version}-py3-none-any.whl"}],
    }
    requests = []
    commands = []
    fail = False

    def upstream(request):
        requests.append(str(request.url))
        return httpx.Response(200, json=data)

    async def run(args):
        commands.append(args)
        if fail:
            raise DependencyError("installer failure")
        if "venv" in args:
            directory = Path(args[-1]) / "bin"
            directory.mkdir()
            (directory / "spotdl").write_text("#!/bin/sh\necho 4.5.2\n")
            (directory / "spotdl").chmod(0o700)

    monkeypatch.setattr(DependencyManager, "run_python_install", staticmethod(run))
    manager = DependencyManager(httpx.MockTransport(upstream))
    settings = Settings.from_env({"DEPENDENCY_DIR": str(tmp_path), "SPOTDL_MODE": "managed-python"})
    first = asyncio.run(manager.install(settings, "spotdl", "latest"))
    assert requests == ["https://pypi.org/pypi/spotdl/json"]
    assert "python-4.5.2-" in first["binary"]
    assert resolve_tool(settings, "spotdl") == first["binary"]
    assert first["source"] == "PyPI/spotdl"
    assert "--only-binary=:all:" in commands[1]
    assert commands[1][-1].endswith("#sha256=" + "a" * 64)
    assert not (tmp_path / "spotdl/current.json").exists()
    fail = True
    with pytest.raises(DependencyError, match="installer failure"):
        asyncio.run(manager.install(settings, "spotdl", "v4.5.2"))
    assert requests[-1] == "https://pypi.org/pypi/spotdl/4.5.2/json"
    assert installed(settings, "spotdl") == first
    assert len(list((tmp_path / "spotdl").glob("python-4.5.2-*"))) == 1


def test_tool_environment_excludes_host_python_paths(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/private-rpm-pythonlibs")
    monkeypatch.setenv("PYTHONHOME", "/other-python")
    monkeypatch.setenv("VIRTUAL_ENV", "/other-venv")
    monkeypatch.setenv("HOME", "/var/lib/plex-source-injection")
    environment = tool_environment()
    assert not {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"} & environment.keys()
    assert environment["HOME"] == "/var/lib/plex-source-injection"


def test_python_spotdl_lists_stable_pypi_wheels():
    def wheel(yanked=False):
        return {"filename": "spotdl-py3-none-any.whl", "yanked": yanked,
                "upload_time_iso_8601": "2026-10-08T00:00:00Z"}
    data = {"releases": {"4.5.2": [wheel()], "4.10.0": [wheel()],
                         "5.0.0rc1": [wheel()], "4.6.0": [wheel(True)],
                         "4.9.0": [{"filename": "spotdl.tar.gz", "yanked": False}]}}
    manager = DependencyManager(httpx.MockTransport(lambda request: httpx.Response(200, json=data)))
    settings = Settings.from_env({"SPOTDL_MODE": "managed-python"})
    releases = asyncio.run(manager.releases("spotdl", settings))
    assert [release["version"] for release in releases] == ["4.10.0", "4.5.2"]


def test_managed_deno_install_extracts_verified_zip(tmp_path, linux):
    executable = b"#!/bin/sh\necho deno 2.9.7\n"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr("deno", executable)
        bundle.writestr("../outside", b"bad")
    payload = buffer.getvalue()
    latest = release("deno", payload, "v2.9.7")
    assert latest["assets"][0]["name"] == "deno-x86_64-unknown-linux-gnu.zip"

    def upstream(request):
        if request.url.host == "api.github.com":
            return httpx.Response(200, json=latest)
        return httpx.Response(200, content=payload)

    settings = Settings.from_env({"DEPENDENCY_DIR": str(tmp_path), "DENO_MODE": "managed"})
    assert deno_location(settings) is None
    info = asyncio.run(DependencyManager(httpx.MockTransport(upstream)).install(settings, "deno", "latest"))
    assert Path(info["binary"]).read_bytes() == executable
    assert info["reported_version"] == "deno 2.9.7"
    assert deno_location(settings) == info["binary"]
    assert not (tmp_path / "outside").exists()
    assert sorted(p.name for p in Path(info["binary"]).parent.iterdir()) == ["deno"]


def test_deno_archive_without_executable_is_rejected(tmp_path):
    archive = tmp_path / "deno.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("bin/deno", b"x")
    with pytest.raises(DependencyError, match="unique safe deno"):
        DependencyManager.extract_deno(archive, tmp_path, "deno")


def test_deno_is_optional_and_added_to_tool_path(monkeypatch):
    monkeypatch.setattr("dependencies.shutil.which", lambda name: None)
    assert deno_location(Settings()) is None
    monkeypatch.setattr("dependencies.shutil.which", lambda name: "/opt/deno/bin/deno")
    assert deno_location(Settings()) == "/opt/deno/bin/deno"
    monkeypatch.setenv("PATH", "/usr/bin")
    assert tool_environment("/opt/deno/bin/deno")["PATH"].split(":")[0] == "/opt/deno/bin"
    assert tool_environment()["PATH"] == "/usr/bin"
