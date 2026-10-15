"""SQLite storage plugin — local development and the offline default.

Persists to ``AIVILLI_DB`` (default ``/tmp/aivilli.db``). ``/tmp`` is
writable on Vercel's read-only filesystem, so this backend keeps working as a
graceful fallback when Cloudflare D1 credentials are absent.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from typing import Any, Sequence

from plugins.storage.sql_base import SQLStoragePlugin, _Exec


class _SQLiteExec(_Exec):
    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._init_pragma()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_pragma(self) -> None:
        with self._conn() as conn:
            conn.execute("PRAGMA journal_mode=WAL;")

    def query(self, sql: str, params: Sequence[Any] = ()) -> list:
        with self._lock, self._conn() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [dict(r) for r in rows]

    def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        with self._lock, self._conn() as conn:
            conn.execute(sql, tuple(params))


class SQLiteStoragePlugin(SQLStoragePlugin):
    name = "sqlite"
    description = "Local SQLite file (default /tmp/aivilli.db); zero-config fallback."

    def __init__(self, path: str | None = None) -> None:
        super().__init__()
        self.path = path or os.environ.get("AIVILLI_DB", "/tmp/aivilli.db")

    def _connect(self) -> _Exec:
        return _SQLiteExec(self.path)

    def health(self) -> dict:
        return {**super().health(), "path": self.path}
