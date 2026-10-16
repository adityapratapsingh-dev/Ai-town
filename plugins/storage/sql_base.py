"""Backend-agnostic SQL storage for aivilli.

Cloudflare D1 speaks SQLite SQL, so a single implementation of the
``StoragePlugin`` contract drives either backend. Subclasses only provide the
low-level ``_Exec`` primitives (``query`` / ``execute``).

Design notes (Moltbook-inspired):
* The platform is **agent-first**. Humans may observe; agents act.
* Content is organised into **hoods** (communities, like Moltbook "submolts").
* Everything runs on **upvotes (+1), downvotes (-1) and karma** — there are no
  likes or hearts anywhere in the schema.
* Posts and comments both carry ``upvotes`` / ``downvotes`` / ``score`` so a
  vote never has to re-aggregate the whole ``votes`` table.
* Comments are **nested**: a comment may reply to the post (``parent_comment_id``
  is NULL) or to another comment (``depth`` = parent depth + 1).
* Agents accumulate **post_karma** and **comment_karma**; ``karma`` is always
  their sum.
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
        provider      TEXT NOT NULL DEFAULT '',
        webhook_url   TEXT,
        claim_token   TEXT,
        owner         TEXT,
        post_karma    INTEGER NOT NULL DEFAULT 0,
        comment_karma INTEGER NOT NULL DEFAULT 0,
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
        id            TEXT PRIMARY KEY,
        author        TEXT NOT NULL,
        hood          TEXT NOT NULL DEFAULT 'general',
        title         TEXT NOT NULL DEFAULT '',
        body          TEXT NOT NULL,
        reply_to      TEXT,
        mentions      TEXT NOT NULL DEFAULT '',
        upvotes       INTEGER NOT NULL DEFAULT 0,
        downvotes     INTEGER NOT NULL DEFAULT 0,
        score         INTEGER NOT NULL DEFAULT 0,
        comment_count INTEGER NOT NULL DEFAULT 0,
        model         TEXT NOT NULL DEFAULT '',
        provider      TEXT NOT NULL DEFAULT '',
        created_at    TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS comments (
        id                TEXT PRIMARY KEY,
        post_id           TEXT NOT NULL,
        parent_comment_id TEXT,
        reply_to_handle   TEXT NOT NULL DEFAULT '',
        depth             INTEGER NOT NULL DEFAULT 0,
        author            TEXT NOT NULL,
        body              TEXT NOT NULL,
        mentions          TEXT NOT NULL DEFAULT '',
        upvotes           INTEGER NOT NULL DEFAULT 0,
        downvotes         INTEGER NOT NULL DEFAULT 0,
        score             INTEGER NOT NULL DEFAULT 0,
        reply_count       INTEGER NOT NULL DEFAULT 0,
        model             TEXT NOT NULL DEFAULT '',
        provider          TEXT NOT NULL DEFAULT '',
        created_at        TEXT NOT NULL
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
        id                TEXT PRIMARY KEY,
        handle            TEXT NOT NULL,
        kind              TEXT NOT NULL,
        actor             TEXT NOT NULL,
        post_id           TEXT,
        comment_id        TEXT,
        parent_comment_id TEXT,
        hood              TEXT NOT NULL DEFAULT '',
        preview_text      TEXT NOT NULL DEFAULT '',
        read              INTEGER NOT NULL DEFAULT 0,
        created_at        TEXT NOT NULL
    )
    """,
]

INDEX_STATEMENTS = [
    "CREATE INDEX IF NOT EXISTS idx_posts_author ON posts (author, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_posts_hood ON posts (hood, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_posts_reply ON posts (reply_to, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_posts_created ON posts (created_at)",
    "CREATE INDEX IF NOT EXISTS idx_posts_score ON posts (score)",
    "CREATE INDEX IF NOT EXISTS idx_comments_post ON comments (post_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_comments_parent ON comments (parent_comment_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_comments_author ON comments (author, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_follows_follower ON follows (follower)",
    "CREATE INDEX IF NOT EXISTS idx_follows_followee ON follows (followee)",
    "CREATE INDEX IF NOT EXISTS idx_votes_post ON votes (post_id)",
    "CREATE INDEX IF NOT EXISTS idx_votes_handle ON votes (handle)",
    "CREATE INDEX IF NOT EXISTS idx_hood_members ON hood_members (handle)",
    "CREATE INDEX IF NOT EXISTS idx_hood_members_hood ON hood_members (hood)",
    # Presence and inbox are polled constantly: both want a covering index.
    "CREATE INDEX IF NOT EXISTS idx_agents_last_seen ON agents (last_seen)",
    "CREATE INDEX IF NOT EXISTS idx_agents_kind ON agents (kind)",
    "CREATE INDEX IF NOT EXISTS idx_notif_handle ON notifications (handle, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_notif_unread ON notifications (handle, read)",
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
    ("agents", "provider", "ALTER TABLE agents ADD COLUMN provider TEXT NOT NULL DEFAULT ''"),
    ("agents", "post_karma", "ALTER TABLE agents ADD COLUMN post_karma INTEGER NOT NULL DEFAULT 0"),
    ("agents", "comment_karma", "ALTER TABLE agents ADD COLUMN comment_karma INTEGER NOT NULL DEFAULT 0"),
    ("posts", "hood", "ALTER TABLE posts ADD COLUMN hood TEXT NOT NULL DEFAULT 'general'"),
    ("posts", "title", "ALTER TABLE posts ADD COLUMN title TEXT NOT NULL DEFAULT ''"),
    ("posts", "model", "ALTER TABLE posts ADD COLUMN model TEXT NOT NULL DEFAULT ''"),
    ("posts", "provider", "ALTER TABLE posts ADD COLUMN provider TEXT NOT NULL DEFAULT ''"),
    ("posts", "mentions", "ALTER TABLE posts ADD COLUMN mentions TEXT NOT NULL DEFAULT ''"),
    ("posts", "upvotes", "ALTER TABLE posts ADD COLUMN upvotes INTEGER NOT NULL DEFAULT 0"),
    ("posts", "downvotes", "ALTER TABLE posts ADD COLUMN downvotes INTEGER NOT NULL DEFAULT 0"),
    ("posts", "score", "ALTER TABLE posts ADD COLUMN score INTEGER NOT NULL DEFAULT 0"),
    ("posts", "comment_count", "ALTER TABLE posts ADD COLUMN comment_count INTEGER NOT NULL DEFAULT 0"),
    ("notifications", "comment_id", "ALTER TABLE notifications ADD COLUMN comment_id TEXT"),
    ("notifications", "parent_comment_id", "ALTER TABLE notifications ADD COLUMN parent_comment_id TEXT"),
    ("notifications", "hood", "ALTER TABLE notifications ADD COLUMN hood TEXT NOT NULL DEFAULT ''"),
    ("notifications", "preview_text", "ALTER TABLE notifications ADD COLUMN preview_text TEXT NOT NULL DEFAULT ''"),
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

# Short provider labels for the model pill ("Groq · Llama-3.3-70B").
PROVIDER_LABELS = {
    "groq": "Groq",
    "gemini": "Gemini",
    "openai": "OpenAI",
    "openrouter": "OpenRouter",
    "external": "External",
    "simulation": "Simulation",
}


def model_label(model: Optional[str], author_kind: Optional[str] = None) -> str:
    """A short pill label for whoever wrote a post."""
    if author_kind == "human":
        return "Human"
    if model == "simulation":
        return "Simulation"
    if not model:
        # An external agent that posted through the API (no model recorded).
        return "External-Agent"
    if model in _MODEL_LABELS:
        return _MODEL_LABELS[model]
    tail = model.split("/")[-1]
    return tail.replace("-", " ").replace("_", " ").title()


def provider_label(provider: Optional[str]) -> str:
    if not provider:
        return ""
    return PROVIDER_LABELS.get(provider, provider.title())


def public_agent(row: Optional[Dict[str, Any]], **extra: Any) -> Optional[Dict[str, Any]]:
    """Strip secrets and expose the display shape the API/UI expects.

    Handles legacy/partial rows gracefully: a row written before a column
    existed simply reports the default for it.
    """
    if row is None:
        return None
    out = {k: v for k, v in row.items() if k not in _SECRET_FIELDS}
    out["claimed"] = bool(row.get("owner"))
    # The requested API shape: `name`, `avatar`, `id`, `owner_name`.
    out["name"] = row.get("display_name") or row.get("handle") or ""
    out["avatar"] = row.get("avatar_url") or ""
    out["id"] = row.get("handle")
    out["owner_name"] = row.get("owner")
    out["post_karma"] = int(row.get("post_karma") or 0)
    out["comment_karma"] = int(row.get("comment_karma") or 0)
    out["karma"] = int(row.get("karma") or 0)
    out["provider"] = row.get("provider") or ""
    out["provider_label"] = provider_label(row.get("provider"))
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
        self._backfill_denormalised()

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

    def _backfill_denormalised(self) -> None:
        """Recompute derived columns for rows written before they existed.

        Only touches rows that look un-set, so it is cheap and idempotent: a
        post with a real score is never recomputed, and the karma buckets are
        only rebuilt when they disagree with the total.
        """
        try:
            # agents.karma must equal post_karma + comment_karma.
            self.db.execute(
                "UPDATE agents SET karma = post_karma + comment_karma"
                " WHERE karma <> post_karma + comment_karma"
            )
        except Exception:
            pass
        try:
            # posts: rebuild upvotes/downvotes for legacy rows, then score.
            self.db.execute(
                """
                UPDATE posts SET
                    upvotes = COALESCE((
                        SELECT COUNT(*) FROM votes v
                        WHERE v.post_id = posts.id AND v.value > 0), 0),
                    downvotes = COALESCE((
                        SELECT COUNT(*) FROM votes v
                        WHERE v.post_id = posts.id AND v.value < 0), 0)
                WHERE upvotes = 0 AND downvotes = 0 AND score = 0
                  AND EXISTS (SELECT 1 FROM votes v WHERE v.post_id = posts.id)
                """
            )
            self.db.execute("UPDATE posts SET score = upvotes - downvotes")
        except Exception:
            pass
        try:
            # posts.comment_count, from the comments table when it has rows.
            self.db.execute(
                """
                UPDATE posts SET comment_count = COALESCE((
                    SELECT COUNT(*) FROM comments c WHERE c.post_id = posts.id), 0)
                WHERE comment_count = 0
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
        provider: str = "",
        webhook_url: Optional[str] = None,
        claim_token: Optional[str] = None,
        owner: Optional[str] = None,
        post_karma: int = 0,
        comment_karma: int = 0,
    ) -> Dict[str, Any]:
        ts = now_iso()
        karma = int(post_karma) + int(comment_karma)
        self.db.execute(
            """
            INSERT INTO agents
                (handle, display_name, kind, bio, avatar_url, password_hash,
                 api_key_hash, model, provider, webhook_url, claim_token, owner,
                 post_karma, comment_karma, karma, created_at, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (handle, display_name or handle, kind, bio, avatar_url,
             password_hash, api_key_hash, model, provider, webhook_url,
             claim_token, owner, int(post_karma), int(comment_karma), karma,
             ts, ts),
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

    def add_karma(self, handle: str, delta: int, *, kind: str = "post") -> int:
        """Move an agent's karma by ``delta``.

        ``kind`` selects which bucket moves (``"post"`` or ``"comment"``) and
        the total is recomputed from the two buckets. The delta is added to
        ``karma`` explicitly because SQLite evaluates every SET expression
        against the *original* row — writing ``karma = post_karma +
        comment_karma`` here would read the pre-update bucket and drift.
        """
        column = "comment_karma" if kind == "comment" else "post_karma"
        self.db.execute(
            f"UPDATE agents SET {column} = {column} + ?,"
            f" karma = post_karma + comment_karma + ? WHERE handle = ?",
            (int(delta), int(delta), handle),
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
        self, author: str, body: str, *, hood: str = DEFAULT_HOOD, title: str = "",
        mentions: Optional[List[str]] = None,
        model: str = "", provider: str = "",
    ) -> Dict[str, Any]:
        post_id = uuid.uuid4().hex
        created = now_iso()
        mention_csv = ",".join(mentions or [])
        self.db.execute(
            "INSERT INTO posts (id, author, hood, title, body, mentions, upvotes,"
            " downvotes, score, comment_count, model, provider, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, 0, 0, 0, 0, ?, ?, ?)",
            (post_id, author, hood or DEFAULT_HOOD, title, body, mention_csv,
             model, provider, created),
        )
        return {
            "id": post_id, "author": author, "hood": hood or DEFAULT_HOOD,
            "title": title, "body": body, "mentions": mention_csv,
            "upvotes": 0, "downvotes": 0, "score": 0, "comment_count": 0,
            "model": model, "provider": provider, "created_at": created,
        }

    def get_post(self, post_id: str) -> Optional[Dict[str, Any]]:
        rows = self.db.query("SELECT * FROM posts WHERE id = ?", (post_id,))
        return rows[0] if rows else None

    def delete_post(self, post_id: str) -> None:
        self.db.execute("DELETE FROM comments WHERE post_id = ?", (post_id,))
        self.db.execute("DELETE FROM votes WHERE post_id = ?", (post_id,))
        self.db.execute("DELETE FROM posts WHERE id = ?", (post_id,))

    def bump_comment_count(self, post_id: str, delta: int = 1) -> None:
        """Move ``posts.comment_count`` by ``delta``, never below zero."""
        self.db.execute(
            "UPDATE posts SET comment_count = MAX(0, comment_count + ?) WHERE id = ?",
            (int(delta), post_id),
        )

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
        sql = f"SELECT * FROM posts WHERE {clause} ORDER BY {order} LIMIT ?"
        params.append(limit)
        return self.db.query(sql, tuple(params))

    def posts_by(self, handle: str, limit: int = 50) -> List[Dict[str, Any]]:
        return self.db.query(
            "SELECT * FROM posts WHERE author = ? ORDER BY created_at DESC, id DESC LIMIT ?",
            (handle, limit),
        )

    def replies_to(self, post_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        """Direct comments on a post (depth 0), oldest first."""
        return self.db.query(
            "SELECT * FROM comments WHERE post_id = ? AND parent_comment_id IS NULL"
            " ORDER BY created_at ASC, id ASC LIMIT ?",
            (post_id, limit),
        )

    # --- comments (nested) ---------------------------------------------
    def create_comment(
        self, post_id: str, author: str, body: str, *,
        parent_comment_id: Optional[str] = None,
        reply_to_handle: str = "",
        depth: int = 0,
        mentions: Optional[List[str]] = None,
        model: str = "", provider: str = "",
    ) -> Dict[str, Any]:
        comment_id = uuid.uuid4().hex
        created = now_iso()
        mention_csv = ",".join(mentions or [])
        self.db.execute(
            "INSERT INTO comments (id, post_id, parent_comment_id, reply_to_handle,"
            " depth, author, body, mentions, upvotes, downvotes, score, reply_count,"
            " model, provider, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, 0, ?, ?, ?)",
            (comment_id, post_id, parent_comment_id, reply_to_handle, int(depth),
             author, body, mention_csv, model, provider, created),
        )
        return {
            "id": comment_id, "post_id": post_id,
            "parent_comment_id": parent_comment_id,
            # Kept for backward compatibility: a comment's root is its post.
            "reply_to": post_id,
            "reply_to_handle": reply_to_handle, "depth": int(depth),
            "author": author, "body": body, "mentions": mention_csv,
            "upvotes": 0, "downvotes": 0, "score": 0, "reply_count": 0,
            "model": model, "provider": provider, "created_at": created,
        }

    def get_comment(self, comment_id: str) -> Optional[Dict[str, Any]]:
        rows = self.db.query("SELECT * FROM comments WHERE id = ?", (comment_id,))
        return rows[0] if rows else None

    def delete_comment(self, comment_id: str) -> None:
        self.db.execute("DELETE FROM votes WHERE post_id = ?", (comment_id,))
        self.db.execute("DELETE FROM comments WHERE id = ?", (comment_id,))

    def comments_for(self, post_id: str, limit: int = 500) -> List[Dict[str, Any]]:
        """Every comment on a post, chronological (flat)."""
        return self.db.query(
            "SELECT * FROM comments WHERE post_id = ?"
            " ORDER BY created_at ASC, id ASC LIMIT ?",
            (post_id, limit),
        )

    def child_comments(self, comment_id: str, limit: int = 200) -> List[Dict[str, Any]]:
        return self.db.query(
            "SELECT * FROM comments WHERE parent_comment_id = ?"
            " ORDER BY created_at ASC, id ASC LIMIT ?",
            (comment_id, limit),
        )

    def comments_by(self, handle: str, limit: int = 50) -> List[Dict[str, Any]]:
        return self.db.query(
            "SELECT * FROM comments WHERE author = ? ORDER BY created_at DESC, id DESC LIMIT ?",
            (handle, limit),
        )

    def bump_reply_count(self, comment_id: str, delta: int = 1) -> None:
        self.db.execute(
            "UPDATE comments SET reply_count = MAX(0, reply_count + ?) WHERE id = ?",
            (int(delta), comment_id),
        )

    def update_comment(self, comment_id: str, body: str, mentions: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
        """Edit a comment's body (and its parsed mentions). Author-checked by the
        caller; this only writes the columns that can change."""
        mention_csv = ",".join(mentions or [])
        self.db.execute(
            "UPDATE comments SET body = ?, mentions = ? WHERE id = ?",
            (body, mention_csv, comment_id),
        )
        return self.get_comment(comment_id)

    def remove_comment(self, comment_id: str) -> Dict[str, int]:
        """Delete a comment **and its descendants**, keeping counters honest.

        Returns how many comments were removed. The post's ``comment_count`` and
        the parent's ``reply_count`` are decremented so a deleted subtree never
        leaves the numbers over-counting (which the old ``delete_comment`` did).
        """
        rows = self.db.query("SELECT id, post_id, parent_comment_id FROM comments")
        by_parent: Dict[Optional[str], List[str]] = {}
        meta: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            meta[r["id"]] = r
            by_parent.setdefault(r.get("parent_comment_id"), []).append(r["id"])
        if comment_id not in meta:
            return {"removed": 0}
        # Breadth-first collection of the whole subtree.
        doomed: List[str] = []
        stack = [comment_id]
        while stack:
            cid = stack.pop()
            doomed.append(cid)
            stack.extend(by_parent.get(cid, []))
        root = meta[comment_id]
        for cid in doomed:
            self.db.execute("DELETE FROM votes WHERE post_id = ?", (cid,))
            self.db.execute("DELETE FROM comments WHERE id = ?", (cid,))
        self.bump_comment_count(root["post_id"], -len(doomed))
        if root.get("parent_comment_id"):
            self.bump_reply_count(root["parent_comment_id"], -1)
        return {"removed": len(doomed)}

    # --- mentions (who named whom) -------------------------------------
    # The ``mentions`` column stores a comma-separated handle list. Matching
    # ``,handle,`` against ``',' || mentions || ','`` avoids the classic false
    # positive where ``@amy`` matches inside ``@amygdala``. Both posts and
    # comments are searched, so a profile can show *every* place you were named.
    def posts_mentioning(self, handle: str, limit: int = 50) -> List[Dict[str, Any]]:
        needle = f"%,{handle},%"
        return self.db.query(
            "SELECT * FROM posts WHERE (',' || mentions || ',') LIKE ?"
            " ORDER BY created_at DESC, id DESC LIMIT ?",
            (needle, limit),
        )

    def comments_mentioning(self, handle: str, limit: int = 50) -> List[Dict[str, Any]]:
        needle = f"%,{handle},%"
        return self.db.query(
            "SELECT * FROM comments WHERE (',' || mentions || ',') LIKE ?"
            " ORDER BY created_at DESC, id DESC LIMIT ?",
            (needle, limit),
        )

    def count_posts_mentioning(self, handle: str) -> int:
        needle = f"%,{handle},%"
        return self._count(
            "SELECT COUNT(*) FROM posts WHERE (',' || mentions || ',') LIKE ?",
            (needle,),
        )

    def count_comments_mentioning(self, handle: str) -> int:
        needle = f"%,{handle},%"
        return self._count(
            "SELECT COUNT(*) FROM comments WHERE (',' || mentions || ',') LIKE ?",
            (needle,),
        )

    # --- per-agent aggregates (one query) ------------------------------
    def counts_for(self, handle: str) -> Dict[str, int]:
        """Everything the profile header needs, in ONE round-trip.

        Posts, comments, upvotes received, downvotes received, followers and
        following are correlated sub-selects so a profile view costs one query
        even on D1 (where each query is an HTTP round-trip).
        """
        rows = self.db.query(
            """
            SELECT
              (SELECT COUNT(*) FROM posts WHERE author = ? AND reply_to IS NULL) AS posts,
              (SELECT COUNT(*) FROM comments WHERE author = ?)                   AS comments,
              (SELECT COUNT(*) FROM follows WHERE followee = ?)                  AS followers,
              (SELECT COUNT(*) FROM follows WHERE follower = ?)                  AS following,
              (SELECT COALESCE(SUM(upvotes), 0) FROM posts WHERE author = ?)     AS post_upvotes,
              (SELECT COALESCE(SUM(downvotes), 0) FROM posts WHERE author = ?)   AS post_downvotes,
              (SELECT COALESCE(SUM(upvotes), 0) FROM comments WHERE author = ?)  AS comment_upvotes,
              (SELECT COALESCE(SUM(downvotes), 0) FROM comments WHERE author = ?) AS comment_downvotes,
              (SELECT COUNT(*) FROM posts WHERE (',' || mentions || ',') LIKE ?) AS mentions_in_posts,
              (SELECT COUNT(*) FROM comments WHERE (',' || mentions || ',') LIKE ?) AS mentions_in_comments
            """,
            (handle, handle, handle, handle, handle, handle, handle, handle,
             f"%,{handle},%", f"%,{handle},%"),
        )
        row = rows[0] if rows else {}
        return {k: int(v or 0) for k, v in row.items()}

    # --- scoped notifications ------------------------------------------
    def notifications_for(
        self, handle: str, limit: int = 50, *, kinds: Optional[List[str]] = None,
        unread_only: bool = False,
    ) -> List[Dict[str, Any]]:
        """A handle's inbox, optionally narrowed by kind and/or unread state.

        This is the *only* correct way to build a personal inbox: it filters on
        ``handle = ?`` in SQL, so a viewer can never see a notification that was
        not addressed to them.
        """
        where = ["handle = ?"]
        params: List[Any] = [handle]
        if unread_only:
            where.append("read = 0")
        if kinds:
            where.append("kind IN (" + ",".join("?" for _ in kinds) + ")")
            params.extend(kinds)
        params.append(limit)
        return self.db.query(
            f"SELECT * FROM notifications WHERE {' AND '.join(where)}"
            " ORDER BY created_at DESC, id DESC LIMIT ?",
            tuple(params),
        )

    def count_notifications_for(
        self, handle: str, *, kinds: Optional[List[str]] = None,
        unread_only: bool = True,
    ) -> int:
        where = ["handle = ?"]
        params: List[Any] = [handle]
        if unread_only:
            where.append("read = 0")
        if kinds:
            where.append("kind IN (" + ",".join("?" for _ in kinds) + ")")
            params.extend(kinds)
        return self._count(
            f"SELECT COUNT(*) FROM notifications WHERE {' AND '.join(where)}",
            tuple(params),
        )

    # --- votes (up/down, on posts AND comments) ------------------------
    def vote(self, handle: str, post_id: str, value: int) -> None:
        """Record a post vote. ``post_id`` is the target id.

        The denormalised ``posts.upvotes`` / ``downvotes`` / ``score`` columns
        are updated by the delta so rendering never needs an aggregate query.
        """
        value = 1 if value > 0 else (-1 if value < 0 else 0)
        previous = self.voter_value(handle, post_id)
        if value == 0:
            if previous:
                self.db.execute(
                    "DELETE FROM votes WHERE handle = ? AND post_id = ?", (handle, post_id)
                )
        else:
            self.db.execute(
                "INSERT INTO votes (handle, post_id, value, created_at) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(handle, post_id) DO UPDATE SET value = excluded.value",
                (handle, post_id, value, now_iso()),
            )
        if previous != value:
            self._apply_tally("posts", "id", post_id, previous, value)

    def _apply_tally(self, table: str, key: str, target_id: str,
                     previous: int, value: int) -> None:
        """Move ``upvotes``/``downvotes``/``score`` by the vote delta."""
        up_delta = (1 if value > 0 else 0) - (1 if previous > 0 else 0)
        down_delta = (1 if value < 0 else 0) - (1 if previous < 0 else 0)
        if not up_delta and not down_delta:
            return
        self.db.execute(
            f"UPDATE {table} SET"
            " upvotes = MAX(0, upvotes + ?),"
            " downvotes = MAX(0, downvotes + ?),"
            " score = MAX(0, upvotes + ?) - MAX(0, downvotes + ?)"
            f" WHERE {key} = ?",
            (up_delta, down_delta, up_delta, down_delta, target_id),
        )

    def vote_count(self, post_id: str) -> int:
        return self._count("SELECT COALESCE(SUM(value),0) FROM votes WHERE post_id = ?", (post_id,))

    def voter_value(self, handle: str, post_id: str) -> int:
        rows = self.db.query(
            "SELECT value FROM votes WHERE handle = ? AND post_id = ?", (handle, post_id)
        )
        return int(rows[0]["value"]) if rows else 0

    def vote_comment(self, handle: str, comment_id: str, value: int) -> None:
        value = 1 if value > 0 else (-1 if value < 0 else 0)
        previous = self.voter_value(handle, comment_id)
        if value == 0:
            if previous:
                self.db.execute(
                    "DELETE FROM votes WHERE handle = ? AND post_id = ?", (handle, comment_id)
                )
        else:
            self.db.execute(
                "INSERT INTO votes (handle, post_id, value, created_at) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(handle, post_id) DO UPDATE SET value = excluded.value",
                (handle, comment_id, value, now_iso()),
            )
        if previous != value:
            self._apply_tally("comments", "id", comment_id, previous, value)

    # --- notifications -------------------------------------------------
    def notify(
        self, handle: str, *, kind: str, actor: str,
        post_id: Optional[str] = None, comment_id: Optional[str] = None,
        parent_comment_id: Optional[str] = None, hood: str = "",
        preview_text: str = "",
    ) -> Optional[Dict[str, Any]]:
        if handle == actor:
            return None  # never notify yourself about your own action
        notif_id = uuid.uuid4().hex
        created = now_iso()
        self.db.execute(
            "INSERT INTO notifications (id, handle, kind, actor, post_id, comment_id,"
            " parent_comment_id, hood, preview_text, read, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
            (notif_id, handle, kind, actor, post_id, comment_id,
             parent_comment_id, hood, preview_text, created),
        )
        return {
            "id": notif_id, "handle": handle, "kind": kind, "actor": actor,
            "post_id": post_id, "comment_id": comment_id,
            "parent_comment_id": parent_comment_id, "hood": hood,
            "preview_text": preview_text, "read": 0, "created_at": created,
        }

    def notifications(self, handle: str, limit: int = 50) -> List[Dict[str, Any]]:
        return self.db.query(
            "SELECT * FROM notifications WHERE handle = ?"
            " ORDER BY created_at DESC, id DESC LIMIT ?",
            (handle, limit),
        )

    def all_notifications(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Every notification on the network (the UI's live feed)."""
        return self.db.query(
            "SELECT * FROM notifications ORDER BY created_at DESC, id DESC LIMIT ?",
            (limit,),
        )

    def get_notification(self, notif_id: str) -> Optional[Dict[str, Any]]:
        rows = self.db.query("SELECT * FROM notifications WHERE id = ?", (notif_id,))
        return rows[0] if rows else None

    def unread_notifications(self, handle: str, limit: int = 50) -> List[Dict[str, Any]]:
        return self.db.query(
            "SELECT * FROM notifications WHERE handle = ? AND read = 0"
            " ORDER BY created_at DESC, id DESC LIMIT ?",
            (handle, limit),
        )

    def unread_count(self, handle: str) -> int:
        return self._count(
            "SELECT COUNT(*) FROM notifications WHERE handle = ? AND read = 0", (handle,)
        )

    def total_unread(self) -> int:
        return self._count("SELECT COUNT(*) FROM notifications WHERE read = 0")

    def mark_notifications_read(
        self, handle: str, ids: Optional[List[str]] = None
    ) -> None:
        """Mark a handle's notifications read — all, or just ``ids``."""
        if ids:
            placeholders = ",".join("?" for _ in ids)
            self.db.execute(
                f"UPDATE notifications SET read = 1 WHERE handle = ?"
                f" AND id IN ({placeholders})",
                (handle, *ids),
            )
        else:
            self.db.execute("UPDATE notifications SET read = 1 WHERE handle = ?", (handle,))

    def mark_notification_read(self, notif_id: str) -> None:
        self.db.execute("UPDATE notifications SET read = 1 WHERE id = ?", (notif_id,))

    # --- stats ---------------------------------------------------------
    def stats(self, online_within_seconds: Optional[float] = None) -> Dict[str, Any]:
        """All counters in ONE query (one D1 round-trip instead of nine).

        When ``online_within_seconds`` is given the online count is folded into
        the same query, so a dashboard read is a single round-trip rather than
        two.
        """
        params: tuple = ()
        online_col = "0 AS online"
        if online_within_seconds is not None:
            cutoff = datetime.now(timezone.utc).timestamp() - online_within_seconds
            cutoff_iso = datetime.fromtimestamp(cutoff, timezone.utc).isoformat()
            online_col = (
                "(SELECT COUNT(*) FROM agents WHERE last_seen IS NOT NULL"
                " AND last_seen >= ?) AS online"
            )
            params = (cutoff_iso,)
        rows = self.db.query(
            f"""
            SELECT
              (SELECT COUNT(*) FROM agents)                              AS agents,
              (SELECT COUNT(*) FROM agents WHERE kind = 'ai')            AS ai,
              (SELECT COUNT(*) FROM agents WHERE kind = 'human')         AS humans,
              (SELECT COUNT(*) FROM agents
                 WHERE kind = 'ai' AND api_key_hash IS NOT NULL)         AS external,
              (SELECT COUNT(*) FROM agents
                 WHERE kind = 'ai' AND api_key_hash IS NULL)             AS resident,
              (SELECT COUNT(*) FROM agents WHERE owner IS NOT NULL)      AS claimed,
              (SELECT COUNT(*) FROM hoods)                               AS hoods,
              (SELECT COUNT(*) FROM posts WHERE reply_to IS NULL)        AS posts,
              (SELECT COUNT(*) FROM comments)                            AS comments,
              (SELECT COUNT(*) FROM votes)                               AS votes,
              (SELECT COALESCE(SUM(karma), 0) FROM agents)               AS karma,
              (SELECT COUNT(*) FROM notifications WHERE read = 0)        AS unread_notifications,
              {online_col}
            """,
            params,
        )
        row = rows[0] if rows else {}
        out = {
            "agents": int(row.get("agents", 0)),
            "ai": int(row.get("ai", 0)),
            "humans": int(row.get("humans", 0)),
            "external": int(row.get("external", 0)),
            "resident": int(row.get("resident", 0)),
            "claimed": int(row.get("claimed", 0)),
            "hoods": int(row.get("hoods", 0)),
            "posts": int(row.get("posts", 0)),
            "comments": int(row.get("comments", 0)),
            "votes": int(row.get("votes", 0)),
            "karma": int(row.get("karma", 0)),
            "unread_notifications": int(row.get("unread_notifications", 0)),
        }
        if online_within_seconds is not None:
            out["online"] = int(row.get("online", 0))
        return out

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
    def _author_map(self, handles: Sequence[str]) -> Dict[str, Dict[str, Any]]:
        handles = [h for h in handles if h]
        if not handles:
            return {}
        placeholders = ",".join("?" for _ in handles)
        return {
            r["handle"]: r
            for r in self.db.query(
                f"SELECT * FROM agents WHERE handle IN ({placeholders})",
                tuple(handles),
            )
        }

    @staticmethod
    def _mention_list(value: Any) -> List[str]:
        if not value:
            return []
        if isinstance(value, list):
            return [str(v) for v in value if v]
        return [m for m in str(value).split(",") if m]

    def decorate_posts(
        self, posts: List[Dict[str, Any]], viewer: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Attach score/reply counts, author info and the viewer's vote."""
        if not posts:
            return posts
        ids = [p["id"] for p in posts]
        placeholders = ",".join("?" for _ in ids)

        authors = self._author_map(list({p["author"] for p in posts}))
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
            post["author_karma"] = int(author.get("karma", 0) or 0)
            post["author_post_karma"] = int(author.get("post_karma", 0) or 0)
            post["author_comment_karma"] = int(author.get("comment_karma", 0) or 0)
            post["author_owner"] = author.get("owner")
            post["author_claimed"] = bool(author.get("owner"))
            post["upvotes"] = int(post.get("upvotes", 0) or 0)
            post["downvotes"] = int(post.get("downvotes", 0) or 0)
            post["score"] = int(post.get("score", 0) or 0)
            post["comment_count"] = int(post.get("comment_count", 0) or 0)
            post["mentions"] = self._mention_list(post.get("mentions"))
            post["viewer_vote"] = viewer_votes.get(post["id"], 0)
            # A human-readable pill for the brain that wrote this post.
            post["model_label"] = model_label(post.get("model"), author.get("kind"))
            post["provider_label"] = provider_label(post.get("provider"))
        return posts

    def decorate_comments(
        self, comments: List[Dict[str, Any]], viewer: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Attach author info and the viewer's vote to a list of comments."""
        if not comments:
            return comments
        ids = [c["id"] for c in comments]
        placeholders = ",".join("?" for _ in ids)
        authors = self._author_map(list({c["author"] for c in comments}))
        viewer_votes: Dict[str, int] = {}
        if viewer:
            viewer_votes = {
                r["post_id"]: int(r["value"])
                for r in self.db.query(
                    f"SELECT post_id, value FROM votes WHERE handle = ? AND post_id IN ({placeholders})",
                    (viewer,) + tuple(ids),
                )
            }
        for comment in comments:
            author = authors.get(comment["author"], {})
            comment["author_name"] = author.get("display_name", comment["author"])
            comment["author_avatar"] = author.get("avatar_url", "")
            comment["author_kind"] = author.get("kind", "ai")
            comment["author_karma"] = int(author.get("karma", 0) or 0)
            comment["author_comment_karma"] = int(author.get("comment_karma", 0) or 0)
            comment["author_claimed"] = bool(author.get("owner"))
            comment["mentions"] = self._mention_list(comment.get("mentions"))
            comment["upvotes"] = int(comment.get("upvotes", 0) or 0)
            comment["downvotes"] = int(comment.get("downvotes", 0) or 0)
            comment["score"] = int(comment.get("score", 0) or 0)
            comment["reply_count"] = int(comment.get("reply_count", 0) or 0)
            comment["viewer_vote"] = viewer_votes.get(comment["id"], 0)
            comment["model_label"] = model_label(comment.get("model"), author.get("kind"))
            comment["provider_label"] = provider_label(comment.get("provider"))
        return comments

    def comment_tree(
        self, post_id: str, viewer: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Build the nested comment tree for a post.

        Returns the root comments (``parent_comment_id IS NULL``), each with a
        ``replies`` array of child comments, recursively. Built from one query
        so a deep thread never becomes N round-trips (important on D1).
        """
        rows = self.decorate_comments(self.comments_for(post_id), viewer)
        by_id: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            row["replies"] = []
            by_id[row["id"]] = row
        roots: List[Dict[str, Any]] = []
        for row in rows:
            parent = row.get("parent_comment_id")
            if parent and parent in by_id:
                by_id[parent]["replies"].append(row)
            else:
                roots.append(row)
        return roots

    def decorate_notifications(
        self, notifications: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Attach the actor's display name/avatar and the API's field names."""
        if not notifications:
            return notifications
        actors = self._author_map(list({n["actor"] for n in notifications}))
        for note in notifications:
            actor = actors.get(note["actor"], {})
            note["actor_name"] = actor.get("display_name", note["actor"])
            note["actor_avatar"] = actor.get("avatar_url", "")
            note["actor_kind"] = actor.get("kind", "ai")
            note["recipient_handle"] = note.get("handle")
            note["actor_handle"] = note.get("actor")
            note["type"] = note.get("kind")
            note["is_read"] = bool(note.get("read"))
        return notifications
