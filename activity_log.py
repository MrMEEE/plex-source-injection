"""Bounded, redacted application activity stored alongside configuration."""

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import datetime, timezone
from typing import Callable, Mapping

from config_store import ConfigStore, is_secret

MAX_ENTRIES = 2000
LOGGERS = ("plex_proxy", "search", "ingest", "cleanup", "runtime", "providers", "dependencies", "plex_login", "web_admin", "admin_access")


class ActivityLog(logging.Handler):
    def __init__(self, store: ConfigStore, values: Callable[[], Mapping[str, str]]) -> None:
        super().__init__(logging.INFO)
        self.store = store
        self.values = values
        self.previous_levels: dict[str, int] = {}
        with store.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS activity_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
                level TEXT NOT NULL, category TEXT NOT NULL, source TEXT NOT NULL, message TEXT NOT NULL
            )""")

    def attach(self) -> None:
        for name in LOGGERS:
            logger = logging.getLogger(name)
            self.previous_levels[name] = logger.level
            logger.setLevel(logging.INFO)
        logging.getLogger().addHandler(self)

    def close(self) -> None:
        logging.getLogger().removeHandler(self)
        for name, level in self.previous_levels.items():
            logging.getLogger(name).setLevel(level)
        super().close()

    def redact(self, message: str) -> str:
        for key, value in self.values().items():
            if is_secret(key) and value:
                message = message.replace(value, "[redacted]")
        message = re.sub(r"(https?://)[^/\s@]+@", r"\1[redacted]@", message)
        message = re.sub(r"(https?://[^\s?]+)\?[^\s]+", r"\1?[redacted]", message)
        message = re.sub(
            r"""(?i)(x-plex-token|access_token|token|password|client_secret|api_key)(["'\s]*[:=]["'\s]*)([^,\s"']+)""",
            r"\1\2[redacted]", message,
        )
        return message[:4000]

    def emit(self, record: logging.LogRecord) -> None:
        if not any(record.name == name or record.name.startswith(name + ".") for name in LOGGERS):
            return
        category = getattr(record, "activity", None)
        if category not in ("search", "download", "system"):
            category = "search" if record.name == "search" else "download" if record.name == "ingest" else "system"
        try:
            message = record.getMessage()
            if record.exc_info and record.exc_info[1]:
                message += f": {type(record.exc_info[1]).__name__}: {record.exc_info[1]}"
            message = self.redact(message)
            timestamp = datetime.fromtimestamp(record.created, timezone.utc).isoformat()
            with self.store.connect() as db:
                db.execute(
                    "INSERT INTO activity_log(timestamp,level,category,source,message) VALUES (?,?,?,?,?)",
                    (timestamp, record.levelname, category, record.name, message),
                )
                db.execute("DELETE FROM activity_log WHERE id <= (SELECT MAX(id) - ? FROM activity_log)", (MAX_ENTRIES,))
        except sqlite3.Error:
            self.handleError(record)

    def entries(self, limit: int, before: int | None, level: str | None, category: str | None) -> list[dict]:
        clauses = []
        params: list[str | int] = []
        for field, value in (("level", level), ("category", category), ("id", before)):
            if value is not None:
                clauses.append(f"{field} {'<' if field == 'id' else '='} ?")
                params.append(value)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.store.connect() as db:
            db.row_factory = sqlite3.Row
            rows = db.execute("SELECT * FROM activity_log" + where + " ORDER BY id DESC LIMIT ?", [*params, limit]).fetchall()
        # Re-redact against current secrets too, including credentials configured since recording.
        return [{**dict(row), "message": self.redact(row["message"])} for row in rows]
