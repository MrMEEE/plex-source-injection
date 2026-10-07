"""Explicit, checksum-verified downloads of versioned upstream tools."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import platform
import re
import shutil
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path

import httpx

from config import Settings


class DependencyError(RuntimeError):
    pass


@dataclass(frozen=True)
class Tool:
    name: str
    repository: str
    mode_key: str
    binary_key: str
    binary: str
    modes: tuple[str, ...] = ("external", "managed")


TOOLS = {
    "spotdl": Tool("spotdl", "spotDL/spotify-downloader", "SPOTDL_MODE", "SPOTDL_BINARY", "spotdl", ("external", "managed", "managed-python")),
    "yt-dlp": Tool("yt-dlp", "yt-dlp/yt-dlp", "YTDLP_MODE", "YTDLP_BINARY", "yt-dlp", ("bundled", "external", "managed")),
    "ffmpeg": Tool("ffmpeg", "yt-dlp/FFmpeg-Builds", "FFMPEG_MODE", "FFMPEG_BINARY", "ffmpeg"),
}
TAG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,100}$")


def tool_environment() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if key not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV")}


def tool_root(settings: Settings, name: str) -> Path:
    return Path(settings.get("DEPENDENCY_DIR", str(Path("dependencies").resolve())) or "").resolve() / name


def installed(settings: Settings, name: str) -> dict[str, str] | None:
    filename = "python-current.json" if name == "spotdl" and settings.get("SPOTDL_MODE") == "managed-python" else "current.json"
    manifest = tool_root(settings, name) / filename
    if not manifest.exists():
        return None
    try:
        data = json.loads(manifest.read_text())
        binary = Path(data["binary"])
        if not binary.is_relative_to(tool_root(settings, name)) or not binary.is_file():
            raise ValueError("Installed executable is missing or outside the tool directory")
        return data
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise DependencyError(f"Invalid {name} installation manifest") from exc


def resolve_tool(settings: Settings, name: str) -> str:
    tool = TOOLS[name]
    mode = settings.get(tool.mode_key, tool.modes[0])
    if mode in ("managed", "managed-python"):
        info = installed(settings, name)
        if not info:
            raise DependencyError(f"{name} is not installed. Open its plugin page and click Install.")
        return info["binary"]
    if mode != "external":
        raise DependencyError(f"{name}: no external executable selected")
    binary = settings.get(tool.binary_key, tool.binary) or tool.binary
    path = shutil.which(binary)
    if not path:
        raise DependencyError(f"{name} executable not found: {binary}")
    return path


def ffmpeg_location(settings: Settings) -> str | None:
    if settings.get("FFMPEG_MODE", "external") == "managed":
        return resolve_tool(settings, "ffmpeg")
    binary = settings.get("FFMPEG_BINARY", "ffmpeg") or "ffmpeg"
    if binary != "ffmpeg":
        return resolve_tool(settings, "ffmpeg")
    return None


class DependencyManager:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.transport = transport
        self.lock = asyncio.Lock()

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=self.transport, follow_redirects=True, timeout=60,
            headers={"Accept": "application/vnd.github+json", "User-Agent": "plex-source-injection"},
        )

    def asset_name(self, name: str, version: str) -> str:
        system, machine = platform.system(), platform.machine().lower()
        if name == "spotdl":
            if machine not in ("x86_64", "amd64"):
                raise DependencyError("Managed spotdl requires x86-64; provide a binary on this architecture.")
            suffix = {"Linux": "linux", "Darwin": "darwin", "Windows": "win32.exe"}.get(system)
            if suffix:
                return f"spotdl-{version.removeprefix('v')}-{suffix}"
        elif name == "yt-dlp":
            if system == "Linux":
                suffix = {"x86_64": "linux", "amd64": "linux", "aarch64": "linux_aarch64", "arm64": "linux_aarch64"}.get(machine)
                if suffix:
                    return f"yt-dlp_{suffix}"
            if system == "Darwin":
                return "yt-dlp_macos"
            if system == "Windows" and machine in ("amd64", "x86_64"):
                return "yt-dlp.exe"
        elif name == "ffmpeg" and system == "Linux":
            suffix = {"x86_64": "linux64", "amd64": "linux64", "aarch64": "linuxarm64", "arm64": "linuxarm64"}.get(machine)
            if suffix:
                return f"ffmpeg-master-latest-{suffix}-gpl.tar.xz"
        raise DependencyError(f"Managed {name} is not available for {system}/{machine}; use an external binary.")

    async def releases(self, name: str, settings: Settings | None = None) -> list[dict[str, str]]:
        tool = TOOLS[name]
        async with self.client() as client:
            if name == "spotdl" and settings is not None and settings.get("SPOTDL_MODE") == "managed-python":
                response = await client.get("https://pypi.org/pypi/spotdl/json")
                response.raise_for_status()
                versions = response.json()["releases"]
                stable = [
                    version for version, assets in versions.items()
                    if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version)
                    and any(asset["filename"].endswith("-py3-none-any.whl") and not asset["yanked"] for asset in assets)
                ]
                stable.sort(key=lambda version: tuple(int(part) for part in version.split(".")), reverse=True)
                return [
                    {"version": version, "published": next(
                        asset["upload_time_iso_8601"] for asset in versions[version]
                        if asset["filename"].endswith("-py3-none-any.whl") and not asset["yanked"]
                    )}
                    for version in stable[:10]
                ]
            response = await client.get(f"https://api.github.com/repos/{tool.repository}/releases", params={"per_page": 10})
            response.raise_for_status()
            releases = response.json()
        return [
            {"version": release["tag_name"], "published": release["published_at"]}
            for release in releases if not release["draft"] and not release["prerelease"]
        ]

    async def install(self, settings: Settings, name: str, version: str) -> dict[str, str]:
        if not TAG.fullmatch(version):
            raise DependencyError("Invalid release version")
        if name == "spotdl" and settings.get("SPOTDL_MODE") == "managed-python":
            return await self.install_python_spotdl(settings, version)
        tool = TOOLS[name]
        async with self.lock, self.client() as client:
            endpoint = "latest" if version == "latest" and name != "ffmpeg" else f"tags/{version}"
            response = await client.get(f"https://api.github.com/repos/{tool.repository}/releases/{endpoint}")
            response.raise_for_status()
            release = response.json()
            version = release["tag_name"]
            if not TAG.fullmatch(version) or release["draft"] or release["prerelease"]:
                raise DependencyError("Release must be a stable upstream version")
            asset_name = self.asset_name(name, version)
            asset = next((asset for asset in release["assets"] if asset["name"] == asset_name), None)
            if not asset:
                raise DependencyError(f"No compatible executable in release {version}")
            digest = asset.get("digest", "")
            if not re.fullmatch(r"sha256:[a-f0-9]{64}", digest or ""):
                raise DependencyError("Release has no published SHA-256 digest; refusing an unverified executable")
            url = asset["browser_download_url"]
            expected_prefix = f"https://github.com/{tool.repository}/releases/download/"
            if not url.startswith(expected_prefix):
                raise DependencyError("Release asset is not from the configured upstream repository")
            root = tool_root(settings, name)
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            with tempfile.TemporaryDirectory(prefix="install-", dir=root) as directory:
                staging = Path(directory)
                archive = staging / "download"
                sha = hashlib.sha256()
                total = 0
                async with client.stream("GET", url, headers={"Accept": "application/octet-stream"}) as download:
                    download.raise_for_status()
                    with archive.open("wb") as output:
                        async for chunk in download.aiter_bytes():
                            total += len(chunk)
                            if total > 300 * 1024 * 1024:
                                raise DependencyError("Download exceeds 300 MiB")
                            sha.update(chunk)
                            output.write(chunk)
                if total != asset["size"] or sha.hexdigest() != digest.split(":")[1]:
                    raise DependencyError("Download size or SHA-256 verification failed")
                executable = tool.binary + (".exe" if platform.system() == "Windows" else "")
                if name == "ffmpeg":
                    try:
                        await asyncio.to_thread(self.extract_ffmpeg, archive, staging)
                    except tarfile.TarError as exc:
                        raise DependencyError("Invalid FFmpeg archive") from exc
                else:
                    archive.rename(staging / executable)
                (staging / executable).chmod(0o700)
                reported = await self.probe(staging / executable, name)
                destination = root / f"{version}-{sha.hexdigest()[:12]}"
                if not destination.exists():
                    staging.rename(destination)
                info = {
                    "version": version, "sha256": sha.hexdigest(),
                    "binary": str(destination / executable),
                    "source": tool.repository,
                    "reported_version": reported,
                }
                fd, temporary = tempfile.mkstemp(prefix="current-", suffix=".json", dir=root)
                try:
                    with os.fdopen(fd, "w") as manifest:
                        json.dump(info, manifest)
                    os.replace(temporary, root / "current.json")
                finally:
                    Path(temporary).unlink(missing_ok=True)
                return info

    async def install_python_spotdl(self, settings: Settings, version: str) -> dict[str, str]:
        requested = version.removeprefix("v")
        if requested != "latest" and not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", requested):
            raise DependencyError("Python spotdl requires a stable MAJOR.MINOR.PATCH version")
        async with self.lock, self.client() as client:
            suffix = "" if requested == "latest" else f"/{requested}"
            response = await client.get(f"https://pypi.org/pypi/spotdl{suffix}/json")
            response.raise_for_status()
            release = response.json()
            version = release["info"]["version"]
            if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
                raise DependencyError("PyPI spotdl version is not a stable release")
            wheel = next((asset for asset in release["urls"] if asset["filename"].endswith("-py3-none-any.whl") and not asset["yanked"]), None)
            if wheel is None:
                raise DependencyError("No stable universal spotdl wheel is available on PyPI")
            digest = wheel["digests"]["sha256"]
            url = wheel["url"]
            if not re.fullmatch(r"[a-f0-9]{64}", digest) or not url.startswith("https://files.pythonhosted.org/packages/"):
                raise DependencyError("Invalid PyPI spotdl wheel URL or SHA-256 digest")
            root = tool_root(settings, "spotdl")
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            # Virtual environments contain absolute shebangs and cannot be renamed.
            destination = Path(tempfile.mkdtemp(prefix=f"python-{version}-", dir=root))
            published = False
            try:
                await self.run_python_install([sys.executable, "-I", "-m", "venv", str(destination)])
                python = destination / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
                await self.run_python_install([
                    str(python), "-I", "-m", "pip", "--isolated", "install",
                    "--index-url", "https://pypi.org/simple", "--only-binary=:all:",
                    "--no-cache-dir", "--disable-pip-version-check",
                    f"spotdl @ {url}#sha256={digest}",
                ])
                binary = destination / ("Scripts/spotdl.exe" if os.name == "nt" else "bin/spotdl")
                reported = await self.probe(binary, "spotdl")
                info = {
                    "version": version, "sha256": digest, "binary": str(binary),
                    "source": "PyPI/spotdl", "reported_version": reported,
                }
                fd, temporary = tempfile.mkstemp(prefix="python-current-", suffix=".json", dir=root)
                try:
                    with os.fdopen(fd, "w") as manifest:
                        json.dump(info, manifest)
                    os.replace(temporary, root / "python-current.json")
                finally:
                    Path(temporary).unlink(missing_ok=True)
                published = True
                return info
            finally:
                if not published:
                    shutil.rmtree(destination)

    @staticmethod
    async def run_python_install(args: list[str]) -> None:
        try:
            process = await asyncio.create_subprocess_exec(
                *args, env=tool_environment(),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            )
        except OSError as exc:
            raise DependencyError(f"Cannot start Python dependency installer: {exc.strerror}") from exc
        try:
            output, _ = await asyncio.wait_for(process.communicate(), timeout=600)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            process.kill()
            await process.wait()
            raise
        if process.returncode:
            detail = output.decode(errors="replace")[-2000:] if output else "No installer output"
            raise DependencyError(f"Python spotdl installation failed: {detail}. Ensure Python venv/pip support is installed. Previous installation remains selected.")

    @staticmethod
    async def probe(binary: Path, name: str) -> str:
        try:
            process = await asyncio.create_subprocess_exec(
                str(binary), "-version" if name == "ffmpeg" else "--version",
                env=tool_environment(),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            )
        except OSError as exc:
            raise DependencyError(f"{name} cannot run on this system: {exc.strerror}; use an external executable") from exc
        try:
            output, _ = await asyncio.wait_for(process.communicate(), timeout=30)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            process.kill()
            await process.wait()
            raise
        if process.returncode or not output:
            detail = output.decode(errors="replace").strip()[-2000:] if output else "No output from executable"
            guidance = ""
            if "GLIBC_" in detail and "not found" in detail:
                guidance = (
                    " The upstream executable requires a newer glibc than this operating system provides."
                    " Select an older compatible release or use an externally installed executable"
                    " built for this OS; do not replace the system glibc."
                )
            raise DependencyError(
                f"{name} version check failed (exit {process.returncode}): {detail}.{guidance}"
                " Previous installation remains selected."
            )
        return output.decode(errors="replace").splitlines()[0][:200]

    @staticmethod
    def extract_ffmpeg(archive: Path, staging: Path) -> None:
        with tarfile.open(archive, "r:xz") as bundle:
            for binary in ("ffmpeg", "ffprobe"):
                members = [
                    member for member in bundle.getmembers()
                    if member.name.endswith(f"/bin/{binary}") and member.isfile()
                ]
                if len(members) != 1 or members[0].size > 512 * 1024 * 1024:
                    raise DependencyError(f"FFmpeg archive has no unique safe {binary} executable")
                source = bundle.extractfile(members[0])
                if source is None:
                    raise DependencyError(f"Cannot read {binary}")
                with source, (staging / binary).open("wb") as output:
                    shutil.copyfileobj(source, output)
                (staging / binary).chmod(0o700)
        archive.unlink()
