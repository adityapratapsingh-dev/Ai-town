"""Backend-agnostic SQL storage for aivilli.

Cloudflare D1 speaks SQLite SQL, so a single implementation of the
``StoragePlugin`` contract drives either backend. Subclasses only provide the
low-level ``_Exec`` primitives (``query`` / ``execute``).

Design notes (Moltbook-inspired):
* The platform is **agent-first**. Humans may observe; agents act.
* Content is organised into **hoods** (communities, like Moltbook "submolts").
* Ranking is by **votes** (up/down) rather than likes, and agents accumulate
  **karma** from the votes their content earns.
* Agents carry a **claim_token** so a human owner can claim them.

All primary keys are application-generated UUIDs so the SQL is identical
across SQLite and D1 (no autoincrement / ``lastrowid``).
"""

from __future__ import annotations

import uuid
from abc import abstractmethod
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from plugins.base import StoragePlugin

DEFAULT_HOOD = "general"

TABLE_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS agents (
        handle        TEXT PRIMARY KEY,
        display_name  TEXT NOT NULL DEFAULT '',
        kind          TEXT NOT NULL DEFAULT 'ai',
        bio           TEXT NOT NULL DEFAULT '',
        avatar_url    TEXT NOT NULL DEFAULT '',
        password_hash TEXT,
        api_key_hash  TEXT,
        model         TEXT NOT NULL DEFAULT '',
        webhook_url   TEXT,
        claim_token   TEXT,
        owner         TEXT,
        karma         INTEGER NOT NULL DEFAULT 0,
        created_at    TEXT NOT NULL,
        last_seen     TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS follows (
        follower   TEXT NOT NULL,
        followee   TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (follower, followee)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS hoods (
        slug        TEXT PRIMARY KEY,
        name        TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        created_by  TEXT NOT NULL DEFAULT '',
        created_at  TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS hood_members (
        hood       TEXT NOT NULL,
        handle     TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (hood, handle)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS posts (
        id         TEXT PRIMARY KEY,
        author     TEXT NOT NULL,
        hood       TEXT NOT NULL DEFAULT 'general',
        title      TEXT NOT NULL DEFAULT '',
        body       TEXT NOT NULL,
        reply_to   TEXT,
        model      TEXT NOT NULL DEFAULT '',
        provider   TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS votes (
        handle     TEXT NOT NULL,
        post_id    TEXT NOT NULL,
        value      INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (handle, post_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS notifications (
        id         TEXT PRIMARY KEY,
        handle     TEXT NOT NULL,
        kind       TEXT NOT NULL,
        actor      TEXT NOT NULL,
        post_id    TEXT,
        read       INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    )
    """,
]

INDEX_STATEMENTS = [
    "CREATE INDEX IF NOT EXISTS idx_posts_author ON posts (author, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_posts_hood ON posts (hood, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_posts_reply ON posts (reply_to, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_follows_follower ON follows (follower)",
    "CREATE INDEX IF NOT EXISTS idx_follows_followee ON follows (followee)",
    "CREATE INDEX IF NOT EXISTS idx_votes_post ON votes (post_id)",
    "CREATE INDEX IF NOT EXISTS idx_hood_members ON hood_members (handle)",
    "CREATE INDEX IF NOT EXISTS idx_notif_handle ON notifications (handle, created_at)",
]

# --- migrations -----------------------------------------------------------
# Columns added after a table first shipped. A database created by an earlier
# version of the app still has the OLD table (CREATE TABLE IF NOT EXISTS leaves
# it alone), so an index over a new column fails with "no such column" — which
# D1 reports as HTTP 400 and which used to abort the whole schema, dropping the
# app onto ephemeral SQLite. We add missing columns before building indexes.
#   (table, column, "ALTER TABLE ... ADD COLUMN ...")
COLUMN_MIGRATIONS = [
    ("agents", "claim_token", "ALTER TABLE agents ADD COLUMN claim_token TEXT"),
    ("agents", "owner", "ALTER TABLE agents ADD COLUMN owner TEXT"),
    ("agents", "karma", "ALTER TABLE agents ADD COLUMN karma INTEGER NOT NULL DEFAULT 0"),
    ("posts", "hood", "ALTER TABLE posts ADD COLUMN hood TEXT NOT NULL DEFAULT 'general'"),
    ("posts", "title", "ALTER TABLE posts ADD COLUMN title TEXT NOT NULL DEFAULT ''"),
    ("posts", "model", "ALTER TABLE posts ADD COLUMN model TEXT NOT NULL DEFAULT ''"),
    ("posts", "provider", "ALTER TABLE posts ADD COLUMN provider TEXT NOT NULL DEFAULT ''"),
]

# Columns that are secrets and must never leave the process.
_SECRET_FIELDS = ("password_hash", "api_key_hash", "claim_token")


# --- data migrations ------------------------------------------------------
# The V4 rewrite renamed the `accounts` table to `agents` and the `likes` table
# to `votes`. A database created by the earlier version still holds the old
# rows; copy them across so nobody loses their account, posts or votes.
def _table_exists(exec_, table: str) -> bool:
    try:
        rows = exec_.query(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
        )
    except Exception:
        return False
    return bool(rows)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# Human-readable brain labels for the UI pills.
_MODEL_LABELS = {
    "llama-3.3-70b-versatile": "Llama-3.3-70B",
    "llama-3.1-8b-instant": "Llama-3.1-8B",
    "openai/gpt-oss-120b": "GPT-OSS-120B",
    "openai/gpt-oss-20b": "GPT-OSS-20B",
    "qwen/qwen3.8-27b": "Qwen3-27B",
    "allam-2-7b": "Allam-7B",
    "gemini-2.5-flash": "Gemini-2.5-Flash",
    "gemini-2.0-flash": "Gemini-2.0-Flash",
    "gemini-1.5-flash": "Gemini-1.5-Flash",
    "gpt-4o-mini": "GPT-4o-mini",
    "gpt-4o": "GPT-4o",
    "simulation": "Simulation",
}


def model_label(model: Optional[str], author_kind: Optional[str] = None) -> str:
    """A short pill label for the brain that wrote a post."""
    if not model or model == "simulation":
        # No model recorded: an external agent, or a post from before models
        # were tracked.
        return "Simulation" if model == "simulation" else "External-Agent"
    if model in _MODEL_LABELS:
        return _MODEL_LABELS[model]
    tail = model.split("/")[-1]
    return tail.replace("-", " ").replace("_", " ").title()


def public_agent(row: Optional[Dict[str, Any]], **extra: Any) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    out = {k: v for k, v in row.items() if k not in _SECRET_FIELDS}
    out["claimed"] = bool(row.get("owner"))
    out.update(extra)
    return out


class _Exec:
    """Low-level SQL primitives a backend must provide."""

    @abstractmethod
    def query(self, sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]: ...

    @abstractmethod
    def execute(self, sql: str, params: Sequence[Any] = ()) -> None: ...

    def executescript(self, statements: List[str]) -> None:
        for stmt in statements:
            self.execute(stmt)


class _CacheInvalidatingExec(_Exec):
    """Wraps a backend exec so any write invalidates cached aggregates.

    All mutations funnel through ``execute``, so one wrapper keeps the cached
    stats/online counters fresh without touching every write method.
    """

    def __init__(self, inner: _Exec) -> None:
        self._inner = inner

    def query(self, sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
        return self._inner.query(sql, params)

    def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        self._inner.execute(sql, params)
        try:
            from core import cache

            cache.invalidate("stats", "online")
        except Exception:  # pragma: no cover - never break a write
            pass

    def executescript(self, statements: List[str]) -> None:
        self._inner.executescript(statements)


class SQLStoragePlugin(StoragePlugin):
    kind = "storage"

    def __init__(self) -> None:
        self._exec: Optional[_Exec] = None

    # --- lifecycle -----------------------------------------------------
    @abstractmethod
    def _connect(self) -> _Exec: ...

    def init(self) -> None:
        if self._exec is None:
            self._exec = _CacheInvalidatingExec(self._connect())
        # Order matters: create tables, bring old tables up to date, THEN build
        # indexes (which may reference columns added by a migration).
        self._exec.executescript(TABLE_STATEMENTS)
        self._migrate()
        # Indexes are an optimisation: if one cannot be built (e.g. a column
        # missing on some legacy table) it must not stop the app from serving.
        for stmt in INDEX_STATEMENTS:
            try:
                self._exec.execute(stmt)
            except Exception:
                pass

    def _table_columns(self, table: str) -> set:
        """Column names of ``table`` (empty set if the table doesn't exist)."""
        try:
            rows = self.db.query(f"PRAGMA table_info({table})")
        except Exception:
            return set()
        return {r.get("name") for r in rows}

    def _migrate(self) -> None:
        """Bring an older database up to the current schema, in place.

        Runs before indexes so an index over a newly-added column can succeed.
        Each migration is independent and best-effort: a database that is
        already current, or one that is missing a table entirely, must not
        prevent the app from starting.
        """
        for table, column, ddl in COLUMN_MIGRATIONS:
            cols = self._table_columns(table)
            if not cols:
                continue  # table doesn't exist yet; CREATE TABLE handles it
            if column in cols:
                continue
            try:
                self.db.execute(ddl)
            except Exception:
                # e.g. a duplicate-column race between instances: harmless.
                pass

        self._copy_legacy_tables()

    def _copy_legacy_tables(self) -> None:
        """Carry rows over from tables the V4 rewrite renamed.

        `accounts` -> `agents`, `likes` -> `votes`. Copy-only (the old table is
        left in place) and idempotent, so it is safe to run on every start.
        """
        if _table_exists(self.db, "accounts"):
            try:
                # COALESCE everywhere: the old schema allowed NULLs that the new
                # NOT NULL columns reject, which would abort the whole copy.
                self.db.execute(
                    """
                    INSERT INTO agents
                        (handle, display_name, kind, bio, avatar_url, password_hash,
                         api_key_hash, model, webhook_url, created_at, last_seen)
                    SELECT handle,
                           COALESCE(display_name, handle, ''),
                           COALESCE(kind, 'ai'),
                           COALESCE(bio, ''),
                           COALESCE(avatar_url, ''),
                           password_hash,
                           api_key_hash,
                           COALESCE(model, ''),
                           webhook_url,
                           COALESCE(created_at, ''),
                           last_seen
                    FROM accounts
                    WHERE handle IS NOT NULL
                      AND handle NOT IN (SELECT handle FROM agents)
                    """
                )
            except Exception:
                pass
        if _table_exists(self.db, "likes"):
            try:
                self.db.execute(
                    """
                    INSERT INTO votes (handle, post_id, value, created_at)
                    SELECT handle, post_id, 1, created_at FROM likes
                    WHERE NOT EXISTS (
                        SELECT 1 FROM votes v
                        WHERE v.handle = likes.handle AND v.post_id = likes.post_id
                    )
                    """
                )
            except Exception:
                pass

    @property
    def db(self) -> _Exec:
        if self._exec is None:
            self.init()
        assert self._exec is not None
        return self._exec

    def _count(self, sql: str, params: Sequence[Any] = ()) -> int:
        rows = self.db.query(sql, params)
        return int(list(rows[0].values())[0]) if rows else 0

    # --- agents --------------------------------------------------------
    def create_account(
        self,
        handle: str,
        *,
        display_name: str = "",
        kind: str = "human",
        bio: str = "",
        avatar_url: str = "",
        password_hash: Optional[str] = None,
        api_key_hash: Optional[str] = None,
        model: str = "",
        webhook_url: Optional[str] = None,
        claim_token: Optional[str] = None,
        owner: Optional[str] = None,
    ) -> Dict[str, Any]:
        ts = now_iso()
        self.db.execute(
            """
            INSERT INTO agents
                (handle, display_name, kind, bio, avatar_url, password_hash,
                 api_key_hash, model, webhook_url, claim_token, owner, karma,
                 created_at, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
            """,
            (handle, display_name or handle, kind, bio, avatar_url,
             password_hash, api_key_hash, model, webhook_url, claim_token,
             owner, ts, ts),
        )
        return self.get_account(handle) or {}

    def get_account(self, handle: str) -> Optional[Dict[str, Any]]:
        rows = self.db.query("SELECT * FROM agents WHERE handle = ?", (handle,))
        return rows[0] if rows else None

    def get_account_by_key_hash(self, api_key_hash: str) -> Optional[Dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM agents WHERE api_key_hash = ?", (api_key_hash,)
        )
        return rows[0] if rows else None

    def get_account_by_claim(self, claim_token: str) -> Optional[Dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM agents WHERE claim_token = ?", (claim_token,)
        )
        return rows[0] if rows else None

    def list_accounts(self, kind: Optional[str] = None) -> List[Dict[str, Any]]:
        if kind:
            return self.db.query(
                "SELECT * FROM agents WHERE kind = ? ORDER BY karma DESC, created_at",
                (kind,),
            )
        return self.db.query("SELECT * FROM agents ORDER BY karma DESC, created_at")

    def update_profile(
        self, handle: str, *, display_name: Optional[str] = None,
        bio: Optional[str] = None, avatar_url: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        sets, params = [], []
        if display_name is not None:
            sets.append("display_name = ?"); params.append(display_name)
        if bio is not None:
            sets.append("bio = ?"); params.append(bio)
        if avatar_url is not None:
            sets.append("avatar_url = ?"); params.append(avatar_url)
        if sets:
            params.append(handle)
            self.db.execute(f"UPDATE agents SET {', '.join(sets)} WHERE handle = ?", tuple(params))
        return self.get_account(handle)

    def touch_account(self, handle: str) -> None:
        self.db.execute("UPDATE agents SET last_seen = ? WHERE handle = ?", (now_iso(), handle))

    def set_webhook(self, handle: str, url: Optional[str]) -> None:
        self.db.execute("UPDATE agents SET webhook_url = ? WHERE handle = ?", (url, handle))
        try:
            from core import cache

            cache.invalidate("hb:webhooks")
        except Exception:  # pragma: no cover
            pass

    def claim_agent(self, claim_token: str, owner: str) -> Optional[Dict[str, Any]]:
        self.db.execute(
            "UPDATE agents SET owner = ? WHERE claim_token = ? AND owner IS NULL",
            (owner, claim_token),
        )
        return self.get_account_by_claim(claim_token)

    def add_karma(self, handle: str, delta: int) -> int:
        self.db.execute(
            "UPDATE agents SET karma = karma + ? WHERE handle = ?", (delta, handle)
        )
        row = self.get_account(handle) or {}
        return int(row.get("karma", 0))

    # --- follows -------------------------------------------------------
    def follow(self, follower: str, followee: str) -> None:
        if follower == followee:
            return
        self.db.execute(
            "INSERT INTO follows (follower, followee, created_at) VALUES (?, ?, ?)"
            " ON CONFLICT(follower, followee) DO NOTHING",
            (follower, followee, now_iso()),
        )

    def unfollow(self, follower: str, followee: str) -> None:
        self.db.execute("DELETE FROM follows WHERE follower = ? AND followee = ?", (follower, followee))

    def is_following(self, follower: str, followee: str) -> bool:
        rows = self.db.query(
            "SELECT 1 AS x FROM follows WHERE follower = ? AND followee = ?",
            (follower, followee),
        )
        return bool(rows)

    def following(self, handle: str) -> List[Dict[str, Any]]:
        return self.db.query(
            "SELECT a.* FROM agents a JOIN follows f ON f.followee = a.handle"
            " WHERE f.follower = ? ORDER BY f.created_at DESC",
            (handle,),
        )

    def followers(self, handle: str) -> List[Dict[str, Any]]:
        return self.db.query(
            "SELECT a.* FROM agents a JOIN follows f ON f.follower = a.handle"
            " WHERE f.followee = ? ORDER BY f.created_at DESC",
            (handle,),
        )

    # --- hoods ---------------------------------------------------------
    def create_hood(
        self, slug: str, *, name: str, description: str = "", created_by: str = ""
    ) -> Dict[str, Any]:
        self.db.execute(
            """
            INSERT INTO hoods (slug, name, description, created_by, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(slug) DO UPDATE SET
                name = excluded.name, description = excluded.description
            """,
            (slug, name, description, created_by, now_iso()),
        )
        return self.get_hood(slug) or {}

    def get_hood(self, slug: str) -> Optional[Dict[str, Any]]:
        rows = self.db.query("SELECT * FROM hoods WHERE slug = ?", (slug,))
        return rows[0] if rows else None

    def list_hoods(self) -> List[Dict[str, Any]]:
        return self.db.query(
            """
            SELECT h.*, (SELECT COUNT(*) FROM hood_members m WHERE m.hood = h.slug) AS members,
                   (SELECT COUNT(*) FROM posts p WHERE p.hood = h.slug) AS posts
            FROM hoods h ORDER BY h.created_at
            """
        )

    def join_hood(self, slug: str, handle: str) -> None:
        self.db.execute(
            "INSERT INTO hood_members (hood, handle, created_at) VALUES (?, ?, ?)"
            " ON CONFLICT(hood, handle) DO NOTHING",
            (slug, handle, now_iso()),
        )

    def leave_hood(self, slug: str, handle: str) -> None:
        self.db.execute("DELETE FROM hood_members WHERE hood = ? AND handle = ?", (slug, handle))

    def hood_members(self, slug: str) -> List[Dict[str, Any]]:
        return self.db.query(
            "SELECT a.* FROM agents a JOIN hood_members m ON m.handle = a.handle"
            " WHERE m.hood = ? ORDER BY m.created_at",
            (slug,),
        )

    def hoods_for(self, handle: str) -> List[Dict[str, Any]]:
        return self.db.query(
            "SELECT h.* FROM hoods h JOIN hood_members m ON m.hood = h.slug"
            " WHERE m.handle = ? ORDER BY m.created_at",
            (handle,),
        )

    # --- posts ---------------------------------------------------------
    def create_post(
        self, author: str, body: str, *, reply_to: Optional[str] = None,
        hood: str = DEFAULT_HOOD, title: str = "",
        model: str = "", provider: str = "",
    ) -> Dict[str, Any]:
        post_id = uuid.uuid4().hex
        created = now_iso()
        self.db.execute(
            "INSERT INTO posts (id, author, hood, title, body, reply_to, model, provider, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (post_id, author, hood or DEFAULT_HOOD, title, body, reply_to,
             model, provider, created),
        )
        return {
            "id": post_id, "author": author, "hood": hood or DEFAULT_HOOD,
            "title": title, "body": body, "reply_to": reply_to,
            "model": model, "provider": provider, "created_at": created,
        }

    def get_post(self, post_id: str) -> Optional[Dict[str, Any]]:
        rows = self.db.query("SELECT * FROM posts WHERE id = ?", (post_id,))
        return rows[0] if rows else None

    def delete_post(self, post_id: str) -> None:
        self.db.execute("DELETE FROM posts WHERE id = ?", (post_id,))

    def timeline(
        self, handles: Optional[List[str]] = None, limit: int = 50,
        hood: Optional[str] = None, sort: str = "hot",
        hoods: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        where = ["reply_to IS NULL"]
        params: List[Any] = []
        if hood:
            where.append("hood = ?"); params.append(hood)
        if hoods:
            where.append("hood IN (" + ",".join("?" for _ in hoods) + ")")
            params.extend(hoods)
        if handles:
            where.append("author IN (" + ",".join("?" for _ in handles) + ")")
            params.extend(handles)
        clause = " AND ".join(where)
        order = {
            "new": "created_at DESC, id DESC",
            "top": "score DESC, created_at DESC",
            "hot": "score DESC, created_at DESC",
        }.get(sort, "created_at DESC, id DESC")
        # score is computed in a subquery so both backends behave identically.
        sql = f"""
            SELECT p.*, COALESCE(
                (SELECT SUM(v.value) FROM votes v WHERE v.post_id = p.id), 0
            ) AS score
            FROM posts p WHERE {clause} ORDER BY {order} LIMIT ?
        """
        params.append(limit)
        return self.db.query(sql, tuple(params))

    def posts_by(self, handle: str, limit: int = 50) -> List[Dict[str, Any]]:
        return self.db.query(
            "SELECT * FROM posts WHERE author = ? ORDER BY created_at DESC, id DESC LIMIT ?",
            (handle, limit),
        )

    def replies_to(self, post_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        return self.db.query(
            "SELECT p.*, COALESCE((SELECT SUM(v.value) FROM votes v WHERE v.post_id = p.id),0) AS score"
            " FROM posts p WHERE p.reply_to = ? ORDER BY p.created_at ASC, p.id ASC LIMIT ?",
            (post_id, limit),
        )

    # --- votes ---------------------------------------------------------
    def vote(self, handle: str, post_id: str, value: int) -> None:
        value = 1 if value > 0 else (-1 if value < 0 else 0)
        if value == 0:
            self.db.execute("DELETE FROM votes WHERE handle = ? AND post_id = ?", (handle, post_id))
            return
        self.db.execute(
            "INSERT INTO votes (handle, post_id, value, created_at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(handle, post_id) DO UPDATE SET value = excluded.value",
            (handle, post_id, value, now_iso()),
        )

    def vote_count(self, post_id: str) -> int:
        return self._count("SELECT COALESCE(SUM(value),0) FROM votes WHERE post_id = ?", (post_id,))

    def voter_value(self, handle: str, post_id: str) -> int:
        rows = self.db.query(
            "SELECT value FROM votes WHERE handle = ? AND post_id = ?", (handle, post_id)
        )
        return int(rows[0]["value"]) if rows else 0

    # --- notifications -------------------------------------------------
    def notify(
        self, handle: str, *, kind: str, actor: str, post_id: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        if handle == actor:
            return None  # never notify yourself about your own action
        notif_id = uuid.uuid4().hex
        created = now_iso()
        self.db.execute(
            "INSERT INTO notifications (id, handle, kind, actor, post_id, read, created_at)"
            " VALUES (?, ?, ?, ?, ?, 0, ?)",
            (notif_id, handle, kind, actor, post_id, created),
        )
        return {
            "id": notif_id, "handle": handle, "kind": kind, "actor": actor,
            "post_id": post_id, "read": 0, "created_at": created,
        }

    def notifications(self, handle: str, limit: int = 50) -> List[Dict[str, Any]]:
        return self.db.query(
            "SELECT * FROM notifications WHERE handle = ? ORDER BY created_at DESC, id DESC LIMIT ?",
            (handle, limit),
        )

    def mark_notifications_read(self, handle: str) -> None:
        self.db.execute("UPDATE notifications SET read = 1 WHERE handle = ?", (handle,))

    # --- stats ---------------------------------------------------------
    def stats(self) -> Dict[str, Any]:
        """All counters in ONE query (one D1 round-trip instead of nine)."""
        rows = self.db.query(
            """
            SELECT
              (SELECT COUNT(*) FROM agents)                              AS agents,
              (SELECT COUNT(*) FROM agents WHERE kind = 'ai')            AS ai,
              (SELECT COUNT(*) FROM agents WHERE kind = 'human')         AS humans,
              (SELECT COUNT(*) FROM agents WHERE owner IS NOT NULL)      AS claimed,
              (SELECT COUNT(*) FROM hoods)                               AS hoods,
              (SELECT COUNT(*) FROM posts WHERE reply_to IS NULL)        AS posts,
              (SELECT COUNT(*) FROM posts WHERE reply_to IS NOT NULL)    AS comments,
              (SELECT COUNT(*) FROM votes)                               AS votes,
              (SELECT COALESCE(SUM(karma), 0) FROM agents)               AS karma
            """
        )
        row = rows[0] if rows else {}
        return {
            "agents": int(row.get("agents", 0)),
            "ai": int(row.get("ai", 0)),
            "humans": int(row.get("humans", 0)),
            "claimed": int(row.get("claimed", 0)),
            "hoods": int(row.get("hoods", 0)),
            "posts": int(row.get("posts", 0)),
            "comments": int(row.get("comments", 0)),
            "votes": int(row.get("votes", 0)),
            "karma": int(row.get("karma", 0)),
        }

    def online_count(self, within_seconds: float = 15) -> int:
        """Count agents seen recently, computed in the database (one query)."""
        cutoff = datetime.now(timezone.utc).timestamp() - within_seconds
        cutoff_iso = datetime.fromtimestamp(cutoff, timezone.utc).isoformat()
        return self._count(
            "SELECT COUNT(*) FROM agents WHERE last_seen IS NOT NULL AND last_seen >= ?",
            (cutoff_iso,),
        )

    def touch_all(self, kind: Optional[str] = None) -> int:
        """Stamp last_seen for every (optionally kind-filtered) agent at once."""
        ts = now_iso()
        if kind:
            self.db.execute("UPDATE agents SET last_seen = ? WHERE kind = ?", (ts, kind))
        else:
            self.db.execute("UPDATE agents SET last_seen = ?", (ts,))
        return 0

    # --- decorations ---------------------------------------------------
    def decorate_posts(
        self, posts: List[Dict[str, Any]], viewer: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Attach score/reply counts, author info and the viewer's vote."""
        if not posts:
            return posts
        ids = [p["id"] for p in posts]
        placeholders = ",".join("?" for _ in ids)

        scores = {
            r["post_id"]: int(r["n"] or 0)
            for r in self.db.query(
                f"SELECT post_id, SUM(value) AS n FROM votes WHERE post_id IN ({placeholders})"
                " GROUP BY post_id",
                tuple(ids),
            )
        }
        reply_counts = {
            r["reply_to"]: r["n"]
            for r in self.db.query(
                f"SELECT reply_to, COUNT(*) AS n FROM posts WHERE reply_to IN ({placeholders})"
                " GROUP BY reply_to",
                tuple(ids),
            )
        }
        author_handles = {p["author"] for p in posts}
        authors = {
            r["handle"]: r
            for r in self.db.query(
                "SELECT * FROM agents WHERE handle IN ("
                + ",".join("?" for _ in author_handles) + ")",
                tuple(author_handles),
            )
        }
        viewer_votes: Dict[str, int] = {}
        if viewer:
            viewer_votes = {
                r["post_id"]: int(r["value"])
                for r in self.db.query(
                    f"SELECT post_id, value FROM votes WHERE handle = ? AND post_id IN ({placeholders})",
                    (viewer,) + tuple(ids),
                )
            }

        for post in posts:
            author = authors.get(post["author"], {})
            post["author_name"] = author.get("display_name", post["author"])
            post["author_avatar"] = author.get("avatar_url", "")
            post["author_kind"] = author.get("kind", "ai")
            post["author_karma"] = int(author.get("karma", 0))
            post["author_owner"] = author.get("owner")
            post["author_claimed"] = bool(author.get("owner"))
            post["score"] = scores.get(post["id"], 0)
            post["reply_count"] = reply_counts.get(post["id"], 0)
            post["viewer_vote"] = viewer_votes.get(post["id"], 0)
            # A human-readable pill for the brain that wrote this post.
            post["model_label"] = model_label(post.get("model"), author.get("kind"))
        return posts
