"""Release manager: bump VERSION/spec, commit, annotate a tag and atomically push."""

from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = "packaging/plex-source-injection.spec"
VERSION_RE = re.compile(r"v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
TRAILER = "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
IGNORE_PROBES = (
    "config.sqlite3", "config.sqlite3-wal", "config.sqlite3.bak", "secrets.db",
    ".env", ".env.production", ".cache", ".run/proxy.log",
    "dependencies/spotdl/current.json", "dist/test.rpm",
)


class ReleaseError(RuntimeError):
    pass


def parse_version(value: str) -> tuple[int, int, int]:
    match = VERSION_RE.fullmatch(value)
    if match is None:
        raise ReleaseError("Version must be MAJOR.MINOR.PATCH, optionally prefixed with v, without leading zeroes.")
    return int(match[1]), int(match[2]), int(match[3])


class ReleaseManager:
    def __init__(self, root: Path = ROOT, dry_run: bool = False, no_push: bool = False) -> None:
        self.root = root
        self.dry_run = dry_run
        self.no_push = no_push

    def git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", "--no-pager", *args], cwd=self.root, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        if result.returncode:
            raise ReleaseError(f"git {shlex.join(args)} failed:\n{result.stderr.strip()}")
        return result.stdout

    def run(self, mode: str, explicit: str | None) -> None:
        current = parse_version((self.root / "VERSION").read_text().strip())
        version = list(current)
        if explicit:
            version = list(parse_version(explicit))
        else:
            index = {"major": 0, "minor": 1, "patch": 2}[mode]
            version[index] += 1
            version[index + 1:] = [0] * (2 - index)
        if tuple(version) <= current:
            raise ReleaseError("Release version must be newer than VERSION.")
        new = ".".join(map(str, version))
        tag = f"v{new}"
        branch = self.git("branch", "--show-current").strip()
        if branch != "main":
            raise ReleaseError("Releases must be made from main (not a detached HEAD).")
        ignored = self.git("ls-files", "-ci", "--exclude-standard").strip()
        if ignored:
            raise ReleaseError(f"Ignored runtime/build files are tracked; remove them from the index first:\n{ignored}")
        for path in IGNORE_PROBES:
            if not self.git("check-ignore", "--", path).strip():
                raise ReleaseError(f"Missing ignore rule for {path}")
        status = self.git("status", "--porcelain")
        if status:
            if not self.dry_run:
                raise ReleaseError("Working tree must be clean. Commit your reviewed changes first:\n" + status)
            print("WARNING: working tree is dirty; an actual release would refuse to proceed.")
        if self.git("tag", "--list", tag).strip():
            raise ReleaseError(f"Tag {tag} already exists locally.")
        if not self.no_push and not self.dry_run:
            remote = self.git("ls-remote", "--heads", "--tags", "origin",
                              "refs/heads/main", f"refs/tags/{tag}").splitlines()
            if any(line.endswith(f"refs/tags/{tag}") for line in remote):
                raise ReleaseError(f"Tag {tag} already exists on origin.")
            head = self.git("rev-parse", "HEAD").strip()
            remote_main = next((line.split()[0] for line in remote if line.endswith("refs/heads/main")), None)
            if remote_main:
                self.git("merge-base", "--is-ancestor", remote_main, head)
        spec = (self.root / SPEC).read_text()
        spec_version = re.search(r"^Version:\s+(\S+)$", spec, re.MULTILINE)
        if spec_version is None or parse_version(spec_version[1]) != current:
            raise ReleaseError("VERSION and RPM spec Version must match.")
        updated = re.sub(r"^Version:\s+\S+$", f"Version:        {new}", spec, count=1, flags=re.MULTILINE)
        if not self.dry_run:
            name = self.git("config", "user.name").strip()
            email = self.git("config", "user.email").strip()
            if not name or not email:
                raise ReleaseError("Configure git user.name and user.email before releasing.")
            author = f"{name} <{email}>"
        else:
            author = "Git committer"
        date = datetime.now().strftime("%a %b %d %Y")
        entry = f"* {date} {author} - {new}-1\n- Release {new}\n"
        if "%changelog\n" in updated:
            updated = updated.replace("%changelog\n", "%changelog\n" + entry + "\n", 1)
        else:
            updated = updated.rstrip() + "\n\n%changelog\n" + entry
        print(f"Release {'.'.join(map(str, current))} -> {new}")
        print(f"Update VERSION and {SPEC}, including RPM changelog.")
        print(f"Commit release, create annotated tag {tag}.")
        if not self.no_push:
            print(f"Atomically push main and {tag} to origin; GitHub Actions publishes EL9/EL10 RPMs.")
        if self.dry_run:
            print("Dry run: no files, commits, tags or remote refs changed; remote availability was not checked.")
            return
        (self.root / "VERSION").write_text(new + "\n")
        (self.root / SPEC).write_text(updated)
        self.git("diff", "--check")
        self.git("add", "--", "VERSION", SPEC)
        self.git("commit", "-m", f"chore: release {new}\n\n{TRAILER}")
        self.git("tag", "-a", tag, "-m", f"Release {new}")
        if not self.no_push:
            self.git("push", "--atomic", "origin", "HEAD:refs/heads/main", f"refs/tags/{tag}")
        else:
            print(f"Local release prepared. Publish with: git push --atomic origin main {tag}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    for mode in ("patch", "minor", "major"):
        group.add_argument("--" + mode, action="store_true", help=f"{mode.capitalize()} version bump")
    group.add_argument("--version", metavar="X.Y.Z", help="Explicit newer version")
    parser.add_argument("--dry-run", action="store_true", help="Preview only, without writes or network calls")
    parser.add_argument("--no-push", action="store_true", help="Create local commit/tag without publishing")
    args = parser.parse_args()
    try:
        ReleaseManager(dry_run=args.dry_run, no_push=args.no_push).run(
            "major" if args.major else "minor" if args.minor else "patch", args.version,
        )
    except (ReleaseError, OSError) as exc:
        print(f"ERROR: {exc}\nNo automatic rollback is performed. Inspect git status/tags before retrying.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
