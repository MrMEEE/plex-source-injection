import subprocess
from pathlib import Path

import pytest

from tools.release import ReleaseError, ReleaseManager, parse_version

ROOT = Path(__file__).resolve().parent.parent


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repository(tmp_path):
    tmp_path = tmp_path / "project"
    tmp_path.mkdir()
    git(tmp_path, "init", "-b", "main")
    git(tmp_path, "config", "user.name", "Release Test")
    git(tmp_path, "config", "user.email", "release@example.invalid")
    git(tmp_path, "config", "commit.gpgsign", "false")
    git(tmp_path, "config", "tag.gpgsign", "false")
    (tmp_path / "packaging").mkdir()
    (tmp_path / "VERSION").write_text("0.0.0\n")
    (tmp_path / "packaging/plex-source-injection.spec").write_text("Name: test\nVersion:        0.0.0\n")
    (tmp_path / ".gitignore").write_text((ROOT / ".gitignore").read_text())
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "Initial")
    return tmp_path


@pytest.mark.parametrize("mode,explicit,expected", [
    ("patch", None, "0.0.1"), ("minor", None, "0.1.0"),
    ("major", None, "1.0.0"), ("patch", "v2.3.4", "2.3.4"),
])
def test_local_release_versions_and_tags(repository, mode, explicit, expected):
    ReleaseManager(repository, no_push=True).run(mode, explicit)
    assert (repository / "VERSION").read_text().strip() == expected
    spec = (repository / "packaging/plex-source-injection.spec").read_text()
    assert f"Version:        {expected}" in spec
    assert "%changelog" in spec and f"- Release {expected}" in spec
    assert git(repository, "cat-file", "-t", f"v{expected}").strip() == "tag"
    assert "Co-authored-by: Copilot" in git(repository, "log", "-1", "--format=%B")
    assert not git(repository, "status", "--porcelain")


def test_dry_run_does_not_write_even_when_dirty(repository):
    (repository / "uncommitted").write_text("keep")
    before = git(repository, "rev-parse", "HEAD")
    ReleaseManager(repository, dry_run=True).run("minor", None)
    assert (repository / "VERSION").read_text() == "0.0.0\n"
    assert git(repository, "rev-parse", "HEAD") == before
    assert not git(repository, "tag", "--list")


def test_dirty_actual_release_refused(repository):
    (repository / "VERSION").write_text("0.0.1\n")
    with pytest.raises(ReleaseError, match="clean"):
        ReleaseManager(repository, no_push=True).run("patch", None)
    assert not git(repository, "tag", "--list")


def test_tracked_database_refused(repository):
    (repository / "config.sqlite3").write_bytes(b"private")
    git(repository, "add", "-f", "config.sqlite3")
    git(repository, "commit", "-m", "Bad tracked data")
    with pytest.raises(ReleaseError, match="runtime/build files"):
        ReleaseManager(repository, no_push=True).run("patch", None)


def test_missing_ignore_rule_refused(repository):
    (repository / ".gitignore").write_text("")
    git(repository, "commit", "-am", "Remove ignore rules")
    with pytest.raises(ReleaseError, match="check-ignore"):
        ReleaseManager(repository, no_push=True).run("patch", None)


def test_wrong_branch_and_duplicate_tag_refused(repository):
    git(repository, "checkout", "-b", "feature")
    with pytest.raises(ReleaseError, match="main"):
        ReleaseManager(repository, no_push=True).run("patch", None)
    git(repository, "checkout", "main")
    git(repository, "tag", "v0.0.1")
    with pytest.raises(ReleaseError, match="already exists"):
        ReleaseManager(repository, no_push=True).run("patch", None)


@pytest.mark.parametrize("value", ["1.2", "1.2.3-rc1", "01.2.3", "vv1.2.3", "-1.2.3"])
def test_invalid_versions(value):
    with pytest.raises(ReleaseError):
        parse_version(value)


def test_explicit_version_must_increase(repository):
    with pytest.raises(ReleaseError, match="newer"):
        ReleaseManager(repository, no_push=True).run("patch", "0.0.0")


def test_atomic_push_to_local_remote(repository, tmp_path):
    remote = tmp_path / "origin.git"
    remote.mkdir()
    git(remote, "init", "--bare")
    git(repository, "remote", "add", "origin", str(remote))
    git(repository, "push", "origin", "main")
    ReleaseManager(repository).run("minor", None)
    assert git(repository, f"--git-dir={remote}", "rev-parse", "refs/heads/main") == git(repository, "rev-parse", "HEAD")
    assert git(repository, f"--git-dir={remote}", "cat-file", "-t", "v0.1.0").strip() == "tag"
