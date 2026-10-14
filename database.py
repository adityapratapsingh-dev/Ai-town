"""SQLite persistence layer for the AI Town Square.

The database lives in the ephemeral ``/tmp`` directory so the app works on
read-only serverless filesystems (Vercel). Because ``/tmp`` can be wiped
between invocations, ``init_db`` is idempotent and safe to call on every
request: it creates the schema and re-seeds the founding citizens when the
``agents`` table is empty.
"""

import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from agents import DEFAULT_AGENTS

DB_PATH = os.environ.get("AI_SOCIETY_DB", "/tmp/ai_society.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    role        TEXT NOT NULL,
    personality TEXT NOT NULL,
    avatar_url  TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'active'
);

CREATE TABLE IF NOT EXISTS conversations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_name    TEXT NOT NULL,
    message       TEXT NOT NULL,
    target_agent  TEXT,
    timestamp     TEXT NOT NULL
);
"""

# A fresh sqlite3 connection per call keeps things simple and thread-safe; the
# lock only serialises the write-heavy seed path.
_init_lock = threading.Lock()
_initialised = False


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init_db(force: bool = False) -> None:
    """Create the schema (once per process) and seed citizens if empty."""
    global _initialised
    if _initialised and not force:
        return
    with _init_lock:
        if _initialised and not force:
            return
        os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
        with _connect() as conn:
            conn.executescript(_SCHEMA)
            count = conn.execute("SELECT COUNT(*) AS n FROM agents").fetchone()["n"]
            if count == 0:
                conn.executemany(
                    "INSERT INTO agents (name, role, personality, avatar_url, status)"
                    " VALUES (:name, :role, :personality, :avatar_url, :status)",
                    [
                        {
                            "name": a["name"],
                            "role": a["role"],
                            "personality": a["personality"],
                            "avatar_url": a["avatar_url"],
                            "status": a.get("status", "active"),
                        }
                        for a in DEFAULT_AGENTS
                    ],
                )
        _initialised = True


def list_agents() -> List[Dict[str, Any]]:
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, name, role, personality, avatar_url, status"
            " FROM agents ORDER BY id"
        ).fetchall()
    return [dict(r) for r in rows]


def get_agent(name: str) -> Optional[Dict[str, Any]]:
    init_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, name, role, personality, avatar_url, status"
            " FROM agents WHERE name = ?",
            (name,),
        ).fetchone()
    return dict(row) if row else None


def set_status(name: str, status: str) -> None:
    init_db()
    with _connect() as conn:
        conn.execute("UPDATE agents SET status = ? WHERE name = ?", (status, name))


def add_message(
    agent_name: str, message: str, target_agent: Optional[str] = None
) -> Dict[str, Any]:
    """Append a message to the town square and return the stored row."""
    init_db()
    ts = _now()
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO conversations (agent_name, message, target_agent, timestamp)"
            " VALUES (?, ?, ?, ?)",
            (agent_name, message, target_agent, ts),
        )
        row_id = cur.lastrowid
    return {
        "id": row_id,
        "agent_name": agent_name,
        "message": message,
        "target_agent": target_agent,
        "timestamp": ts,
    }


def get_recent_messages(limit: int = 3) -> List[Dict[str, Any]]:
    """Return the most recent ``limit`` messages in chronological order."""
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, agent_name, message, target_agent, timestamp"
            " FROM conversations ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in reversed(rows)]


def get_feed(limit: int = 50) -> List[Dict[str, Any]]:
    """Return the newest ``limit`` messages, newest first."""
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, agent_name, message, target_agent, timestamp"
            " FROM conversations ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def count_messages() -> int:
    init_db()
    with _connect() as conn:
        return conn.execute("SELECT COUNT(*) AS n FROM conversations").fetchone()["n"]
