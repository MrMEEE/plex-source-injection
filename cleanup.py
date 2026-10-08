"""Periodic removal of downloaded files older than ``RETENTION_DAYS``."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

SECONDS_PER_DAY = 86400


def cleanup_downloads(
    download_dir: Path, retention_days: int, now: float | None = None
) -> list[Path]:
    """Delete files in ``download_dir`` (recursively) last modified more than ``retention_days`` ago.

    A ``retention_days`` value of ``0`` or less disables cleanup.
    Returns the list of deleted files.
    """
    if retention_days <= 0 or not download_dir.is_dir():
        return []
    cutoff = (time.time() if now is None else now) - retention_days * SECONDS_PER_DAY
    deleted: list[Path] = []
    for path in download_dir.rglob("*"):
        try:
            if path.is_symlink() or not path.is_file():
                continue
            if path.stat().st_mtime < cutoff:
                path.unlink()
                deleted.append(path)
        except OSError:
            logger.warning("Could not remove %s", path, exc_info=True)
    if deleted:
        for folder in sorted({p.parent for p in deleted}, key=lambda p: len(p.parts), reverse=True):
            while folder != download_dir and folder.is_relative_to(download_dir):
                try:
                    folder.rmdir()
                except OSError:
                    break
                folder = folder.parent
        logger.info("Cleanup removed %d file(s) from %s", len(deleted), download_dir)
    return deleted


async def run_cleanup_loop(
    download_dir: Path,
    retention_days: int,
    interval_seconds: float,
    on_deleted: Callable[[list[Path]], Awaitable[None]] | None = None,
) -> None:
    """Run :func:`cleanup_downloads` immediately and then every ``interval_seconds``."""
    while True:
        try:
            deleted = await asyncio.to_thread(cleanup_downloads, download_dir, retention_days)
            if deleted and on_deleted is not None:
                await on_deleted(deleted)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Cleanup run failed")
        await asyncio.sleep(interval_seconds)
