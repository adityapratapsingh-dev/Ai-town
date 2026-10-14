"""Backend-agnostic SQL storage.

Cloudflare D1 speaks SQLite SQL, so a single implementation of the
``StoragePlugin`` contract can drive either backend. Subclasses only provide
the low-level ``_Exec`` primitives (``query`` / ``execute`` / ``executescript``).

All primary keys are application-generated UUIDs, which keeps the SQL
identical across SQLite and D1 (no autoincrement / ``lastrowid`` needed).
"""

from __future__ import annotations

import uuid
from abc import abstractmethod
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from plugins.base import StoragePlugin

SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS citizens (
        name          TEXT PRIMARY KEY,
        kind          TEXT NOT NULL DEFAULT 'native',
        role          TEXT NOT NULL DEFAULT '',
        personality   TEXT NOT NULL DEFAULT '',
        avatar_url    TEXT NOT NULL DEFAULT '',
        status        TEXT NOT NULL DEFAULT 'active',
        api_key_hash  TEXT,
        webhook_url   TEXT,
        created_at    TEXT NOT NULL,
        last_seen     TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS colonies (
        slug        TEXT PRIMARY KEY,
        name        TEXT NOT NULL,
        purpose     TEXT NOT NULL DEFAULT '',
        created_by  TEXT NOT NULL DEFAULT '',
        created_at  TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS colony_members (
        colony_slug  TEXT NOT NULL,
        citizen     TEXT NOT NULL,
        joined_at   TEXT NOT NULL,
        PRIMARY KEY (colony_slug, citizen)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS messages (
        id           TEXT PRIMARY KEY,
        colony_slug  TEXT NOT NULL,
        author       TEXT NOT NULL,
        message      TEXT NOT NULL,
        target       TEXT,
        kind         TEXT NOT NULL DEFAULT 'chat',
        created_at   TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tasks (
        id           TEXT PRIMARY KEY,
        colony_slug  TEXT NOT NULL,
        title        TEXT NOT NULL,
        detail       TEXT NOT NULL DEFAULT '',
        status       TEXT NOT NULL DEFAULT 'open',
        created_by   TEXT NOT NULL DEFAULT '',
        claimed_by   TEXT,
        created_at   TEXT NOT NULL,
        resolved_at  TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_messages_colony ON messages (colony_slug, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_messages_author ON messages (author, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_members_citizen ON colony_members (citizen)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_colony ON tasks (colony_slug, status)",
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class _Exec:
    """Low-level SQL primitives a backend must provide."""

    @abstractmethod
    def query(self, sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]: ...

    @abstractmethod
    def execute(self, sql: str, params: Sequence[Any] = ()) -> None: ...

    def executescript(self, statements: List[str]) -> None:
        for stmt in statements:
            self.execute(stmt)


class SQLStoragePlugin(StoragePlugin):
    kind = "storage"

    def __init__(self) -> None:
        self._exec: Optional[_Exec] = None

    # --- lifecycle -----------------------------------------------------
    @abstractmethod
    def _connect(self) -> _Exec: ...

    def init(self) -> None:
        if self._exec is None:
            self._exec = self._connect()
        self._exec.executescript(SCHEMA_STATEMENTS)

    @property
    def db(self) -> _Exec:
        if self._exec is None:
            self.init()
        assert self._exec is not None
        return self._exec

    # --- citizens ------------------------------------------------------
    def upsert_citizen(
        self,
        name: str,
        *,
        kind: str = "native",
        role: str = "",
        personality: str = "",
        avatar_url: str = "",
        status: str = "active",
        api_key_hash: Optional[str] = None,
        webhook_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        ts = now_iso()
        self.db.execute(
            """
            INSERT INTO citizens
                (name, kind, role, personality, avatar_url, status,
                 api_key_hash, webhook_url, created_at, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                kind = excluded.kind,
                role = excluded.role,
                personality = excluded.personality,
                avatar_url = excluded.avatar_url,
                status = excluded.status,
                api_key_hash = COALESCE(excluded.api_key_hash, citizens.api_key_hash),
                webhook_url = COALESCE(excluded.webhook_url, citizens.webhook_url),
                last_seen = excluded.last_seen
            """,
            (
                name, kind, role, personality, avatar_url, status,
                api_key_hash, webhook_url, ts, ts,
            ),
        )
        return self.get_citizen(name) or {}

    def get_citizen(self, name: str) -> Optional[Dict[str, Any]]:
        rows = self.db.query("SELECT * FROM citizens WHERE name = ?", (name,))
        return rows[0] if rows else None

    def get_citizen_by_key_hash(self, api_key_hash: str) -> Optional[Dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM citizens WHERE api_key_hash = ?", (api_key_hash,)
        )
        return rows[0] if rows else None

    def list_citizens(self, kind: Optional[str] = None) -> List[Dict[str, Any]]:
        if kind:
            return self.db.query(
                "SELECT * FROM citizens WHERE kind = ? ORDER BY created_at", (kind,)
            )
        return self.db.query("SELECT * FROM citizens ORDER BY created_at")

    def set_citizen_status(self, name: str, status: str) -> None:
        self.db.execute("UPDATE citizens SET status = ? WHERE name = ?", (status, name))

    def touch_citizen(self, name: str) -> None:
        self.db.execute("UPDATE citizens SET last_seen = ? WHERE name = ?", (now_iso(), name))

    # --- colonies ------------------------------------------------------
    def create_colony(
        self, slug: str, *, name: str, purpose: str = "", created_by: str = ""
    ) -> Dict[str, Any]:
        self.db.execute(
            """
            INSERT INTO colonies (slug, name, purpose, created_by, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(slug) DO UPDATE SET
                name = excluded.name, purpose = excluded.purpose
            """,
            (slug, name, purpose, created_by, now_iso()),
        )
        return self.get_colony(slug) or {}

    def get_colony(self, slug: str) -> Optional[Dict[str, Any]]:
        rows = self.db.query("SELECT * FROM colonies WHERE slug = ?", (slug,))
        return rows[0] if rows else None

    def list_colonies(self) -> List[Dict[str, Any]]:
        return self.db.query("SELECT * FROM colonies ORDER BY created_at")

    def add_member(self, colony_slug: str, citizen_name: str) -> None:
        self.db.execute(
            """
            INSERT INTO colony_members (colony_slug, citizen, joined_at)
            VALUES (?, ?, ?)
            ON CONFLICT(colony_slug, citizen) DO NOTHING
            """,
            (colony_slug, citizen_name, now_iso()),
        )

    def list_members(self, colony_slug: str) -> List[Dict[str, Any]]:
        return self.db.query(
            """
            SELECT c.* FROM citizens c
            JOIN colony_members m ON m.citizen = c.name
            WHERE m.colony_slug = ?
            ORDER BY m.joined_at
            """,
            (colony_slug,),
        )

    def colonies_for(self, citizen_name: str) -> List[Dict[str, Any]]:
        return self.db.query(
            """
            SELECT col.* FROM colonies col
            JOIN colony_members m ON m.colony_slug = col.slug
            WHERE m.citizen = ?
            ORDER BY m.joined_at
            """,
            (citizen_name,),
        )

    # --- messages ------------------------------------------------------
    def add_message(
        self,
        colony_slug: str,
        author: str,
        message: str,
        *,
        target: Optional[str] = None,
        kind: str = "chat",
    ) -> Dict[str, Any]:
        msg_id = uuid.uuid4().hex
        created = now_iso()
        self.db.execute(
            """
            INSERT INTO messages
                (id, colony_slug, author, message, target, kind, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (msg_id, colony_slug, author, message, target, kind, created),
        )
        return {
            "id": msg_id,
            "colony_slug": colony_slug,
            "author": author,
            "message": message,
            "target": target,
            "kind": kind,
            "created_at": created,
        }

    def recent_messages(self, colony_slug: str, limit: int = 3) -> List[Dict[str, Any]]:
        rows = self.db.query(
            """
            SELECT * FROM messages WHERE colony_slug = ?
            ORDER BY created_at DESC, id DESC LIMIT ?
            """,
            (colony_slug, limit),
        )
        return list(reversed(rows))

    def feed(
        self, colony_slug: Optional[str] = None, limit: int = 60
    ) -> List[Dict[str, Any]]:
        if colony_slug:
            return self.db.query(
                """
                SELECT * FROM messages WHERE colony_slug = ?
                ORDER BY created_at DESC, id DESC LIMIT ?
                """,
                (colony_slug, limit),
            )
        return self.db.query(
            "SELECT * FROM messages ORDER BY created_at DESC, id DESC LIMIT ?",
            (limit,),
        )

    def messages_for(self, citizen_name: str, limit: int = 50) -> List[Dict[str, Any]]:
        return self.db.query(
            """
            SELECT * FROM messages
            WHERE target = ? OR author = ?
            ORDER BY created_at DESC, id DESC LIMIT ?
            """,
            (citizen_name, citizen_name, limit),
        )

    # --- tasks ---------------------------------------------------------
    def create_task(
        self, colony_slug: str, title: str, *, created_by: str = "", detail: str = ""
    ) -> Dict[str, Any]:
        task_id = uuid.uuid4().hex
        self.db.execute(
            """
            INSERT INTO tasks
                (id, colony_slug, title, detail, status, created_by, created_at)
            VALUES (?, ?, ?, ?, 'open', ?, ?)
            """,
            (task_id, colony_slug, title, detail, created_by, now_iso()),
        )
        return self._get_task(task_id) or {}

    def _get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        rows = self.db.query("SELECT * FROM tasks WHERE id = ?", (task_id,))
        return rows[0] if rows else None

    def list_tasks(
        self, colony_slug: Optional[str] = None, status: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        clauses, params = [], []
        if colony_slug:
            clauses.append("colony_slug = ?")
            params.append(colony_slug)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        return self.db.query(
            f"SELECT * FROM tasks{where} ORDER BY created_at DESC", tuple(params)
        )

    def claim_task(self, task_id: int, citizen_name: str) -> Optional[Dict[str, Any]]:
        self.db.execute(
            """
            UPDATE tasks SET status = 'claimed', claimed_by = ?
            WHERE id = ? AND status = 'open'
            """,
            (citizen_name, task_id),
        )
        return self._get_task(task_id)

    def resolve_task(self, task_id: int) -> Optional[Dict[str, Any]]:
        self.db.execute(
            "UPDATE tasks SET status = 'done', resolved_at = ? WHERE id = ?",
            (now_iso(), task_id),
        )
        return self._get_task(task_id)

    # --- stats ---------------------------------------------------------
    def stats(self) -> Dict[str, Any]:
        def count(sql: str) -> int:
            rows = self.db.query(sql)
            return int(list(rows[0].values())[0]) if rows else 0

        return {
            "citizens": count("SELECT COUNT(*) FROM citizens"),
            "native": count("SELECT COUNT(*) FROM citizens WHERE kind = 'native'"),
            "external": count("SELECT COUNT(*) FROM citizens WHERE kind = 'external'"),
            "colonies": count("SELECT COUNT(*) FROM colonies"),
            "messages": count("SELECT COUNT(*) FROM messages"),
            "tasks_open": count("SELECT COUNT(*) FROM tasks WHERE status = 'open'"),
        }
