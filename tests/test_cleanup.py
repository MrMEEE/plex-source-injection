import os
import time

from cleanup import cleanup_downloads


def test_cleanup_removes_only_old_files(tmp_path):
    now = time.time()
    old = tmp_path / "old [a].mp3"
    new = tmp_path / "new [b].mp3"
    nested = tmp_path / "sub" / "older [c].flac"
    nested.parent.mkdir()
    for path in (old, new, nested):
        path.write_bytes(b"x")
    os.utime(old, (now - 31 * 86400, now - 31 * 86400))
    os.utime(nested, (now - 100 * 86400, now - 100 * 86400))
    os.utime(new, (now - 29 * 86400, now - 29 * 86400))

    deleted = cleanup_downloads(tmp_path, retention_days=30, now=now)

    assert sorted(deleted) == sorted([old, nested])
    assert new.exists() and not old.exists() and not nested.exists()


def test_cleanup_disabled_or_missing_dir(tmp_path):
    path = tmp_path / "f.mp3"
    path.write_bytes(b"x")
    os.utime(path, (0, 0))
    assert cleanup_downloads(tmp_path, retention_days=0) == []
    assert path.exists()
    assert cleanup_downloads(tmp_path / "missing", retention_days=30) == []
