"""SQLite storage plugin — local development and the offline default.

Persists to ``AIVILLI_DB`` (default ``/tmp/aivilli.db``). ``/tmp`` is
writable on Vercel's read-only filesystem, so this backend keeps working as a
graceful fallback when Cloudflare D1 credentials are absent.

Smoothness: a **thread-local connection is reused** instead of opening a fresh
file handle per query. Opening a connection is ~100µs of syscalls and, worse,
the previous code ran without WAL or a busy timeout — so concurrent readers and
a writer on the background heartbeat/scheduler threads could hit
``database is locked`` and stall a request. One connection per thread, with WAL
+ ``busy_timeout`` + ``synchronous=NORMAL``, removes both problems: readers
never block the writer and a lock is waited out rather than raised.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from typing import Any, Sequence

from plugins.storage.sql_base import SQLStoragePlugin, _Exec

#: Pragmas applied to every connection once. Chosen for a read-heavy, write-light
#: social workload: WAL lets readers run during a write; NORMAL keeps durability
#: with far fewer fsyncs than FULL; a busy timeout waits out the rare lock
#: instead of erroring; MEMORY temp tables speed up the comment tree / sorting.
_PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA busy_timeout=5000",
    "PRAGMA foreign_keys=OFF",
    "PRAGMA temp_store=MEMORY",
    "PRAGMA cache_size=-8000",
    "PRAGMA mmap_size=134217728",
)


class _SQLiteExec(_Exec):
    def __init__(self, path: str) -> None:
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._local = threading.local()
        self._lock = threading.Lock()
        self._init_journal()

    def _init_journal(self) -> None:
        """Set WAL once, up front (it is a persistent database property)."""
        conn = self._conn()
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
        finally:
            conn.close()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.path,
            timeout=15,
            # check_same_thread=False is safe because each thread gets its own
            # connection via the thread-local below.
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        for pragma in _PRAGMAS:
            try:
                conn.execute(pragma)
            except sqlite3.Error:  # pragma support varies by build; never fatal
                pass
        return conn

    @property
    def _c(self) -> sqlite3.Connection:
        """This thread's connection, opened once and reused."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._conn()
            self._local.conn = conn
        return conn

    def query(self, sql: str, params: Sequence[Any] = ()) -> list:
        with self._lock:
            rows = self._c.execute(sql, tuple(params)).fetchall()
        return [dict(r) for r in rows]

    def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        with self._lock:
            conn = self._c
            conn.execute(sql, tuple(params))
            conn.commit()


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
