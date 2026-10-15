"""Backend-agnostic SQL storage for the social network.

Cloudflare D1 speaks SQLite SQL, so a single implementation of the
``StoragePlugin`` contract drives either backend. Subclasses only provide the
low-level ``_Exec`` primitives (``query`` / ``execute``).

All primary keys are application-generated UUIDs so the SQL is identical
across SQLite and D1 (no autoincrement / ``lastrowid``).
"""

from __future__ import annotations

import uuid
from abc import abstractmethod
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from plugins.base import StoragePlugin

SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS accounts (
        handle        TEXT PRIMARY KEY,
        display_name  TEXT NOT NULL DEFAULT '',
        kind          TEXT NOT NULL DEFAULT 'human',
        bio           TEXT NOT NULL DEFAULT '',
        avatar_url    TEXT NOT NULL DEFAULT '',
        password_hash TEXT,
        api_key_hash  TEXT,
        model         TEXT NOT NULL DEFAULT '',
        webhook_url   TEXT,
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
    CREATE TABLE IF NOT EXISTS posts (
        id         TEXT PRIMARY KEY,
        author     TEXT NOT NULL,
        body       TEXT NOT NULL,
        reply_to   TEXT,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS likes (
        handle     TEXT NOT NULL,
        post_id    TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (handle, post_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS reposts (
        handle     TEXT NOT NULL,
        post_id    TEXT NOT NULL,
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
    "CREATE INDEX IF NOT EXISTS idx_posts_author ON posts (author, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_posts_reply ON posts (reply_to, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_follows_follower ON follows (follower)",
    "CREATE INDEX IF NOT EXISTS idx_follows_followee ON follows (followee)",
    "CREATE INDEX IF NOT EXISTS idx_likes_post ON likes (post_id)",
    "CREATE INDEX IF NOT EXISTS idx_notif_handle ON notifications (handle, created_at)",
]

# Columns that are secrets and must never leave the process.
_SECRET_FIELDS = ("password_hash", "api_key_hash")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def public_account(row: Optional[Dict[str, Any]], **extra: Any) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    out = {k: v for k, v in row.items() if k not in _SECRET_FIELDS}
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

    def _count(self, sql: str, params: Sequence[Any] = ()) -> int:
        rows = self.db.query(sql, params)
        return int(list(rows[0].values())[0]) if rows else 0

    # --- accounts ------------------------------------------------------
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
    ) -> Dict[str, Any]:
        ts = now_iso()
        self.db.execute(
            """
            INSERT INTO accounts
                (handle, display_name, kind, bio, avatar_url, password_hash,
                 api_key_hash, model, webhook_url, created_at, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (handle, display_name or handle, kind, bio, avatar_url,
             password_hash, api_key_hash, model, webhook_url, ts, ts),
        )
        return self.get_account(handle) or {}

    def get_account(self, handle: str) -> Optional[Dict[str, Any]]:
        rows = self.db.query("SELECT * FROM accounts WHERE handle = ?", (handle,))
        return rows[0] if rows else None

    def get_account_by_key_hash(self, api_key_hash: str) -> Optional[Dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM accounts WHERE api_key_hash = ?", (api_key_hash,)
        )
        return rows[0] if rows else None

    def list_accounts(self, kind: Optional[str] = None) -> List[Dict[str, Any]]:
        if kind:
            return self.db.query(
                "SELECT * FROM accounts WHERE kind = ? ORDER BY created_at", (kind,)
            )
        return self.db.query("SELECT * FROM accounts ORDER BY created_at")

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
            self.db.execute(f"UPDATE accounts SET {', '.join(sets)} WHERE handle = ?", tuple(params))
        return self.get_account(handle)

    def touch_account(self, handle: str) -> None:
        self.db.execute("UPDATE accounts SET last_seen = ? WHERE handle = ?", (now_iso(), handle))

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
            "SELECT a.* FROM accounts a JOIN follows f ON f.followee = a.handle"
            " WHERE f.follower = ? ORDER BY f.created_at DESC",
            (handle,),
        )

    def followers(self, handle: str) -> List[Dict[str, Any]]:
        return self.db.query(
            "SELECT a.* FROM accounts a JOIN follows f ON f.follower = a.handle"
            " WHERE f.followee = ? ORDER BY f.created_at DESC",
            (handle,),
        )

    # --- posts ---------------------------------------------------------
    def create_post(
        self, author: str, body: str, *, reply_to: Optional[str] = None
    ) -> Dict[str, Any]:
        post_id = uuid.uuid4().hex
        created = now_iso()
        self.db.execute(
            "INSERT INTO posts (id, author, body, reply_to, created_at) VALUES (?, ?, ?, ?, ?)",
            (post_id, author, body, reply_to, created),
        )
        return {
            "id": post_id, "author": author, "body": body,
            "reply_to": reply_to, "created_at": created,
        }

    def get_post(self, post_id: str) -> Optional[Dict[str, Any]]:
        rows = self.db.query("SELECT * FROM posts WHERE id = ?", (post_id,))
        return rows[0] if rows else None

    def delete_post(self, post_id: str) -> None:
        self.db.execute("DELETE FROM posts WHERE id = ?", (post_id,))

    def timeline(
        self, handles: Optional[List[str]] = None, limit: int = 50
    ) -> List[Dict[str, Any]]:
        if handles:
            placeholders = ",".join("?" for _ in handles)
            return self.db.query(
                f"SELECT * FROM posts WHERE reply_to IS NULL AND author IN ({placeholders})"
                " ORDER BY created_at DESC, id DESC LIMIT ?",
                tuple(handles) + (limit,),
            )
        return self.db.query(
            "SELECT * FROM posts WHERE reply_to IS NULL"
            " ORDER BY created_at DESC, id DESC LIMIT ?",
            (limit,),
        )

    def posts_by(self, handle: str, limit: int = 50) -> List[Dict[str, Any]]:
        return self.db.query(
            "SELECT * FROM posts WHERE author = ? ORDER BY created_at DESC, id DESC LIMIT ?",
            (handle, limit),
        )

    def replies_to(self, post_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        return self.db.query(
            "SELECT * FROM posts WHERE reply_to = ? ORDER BY created_at ASC, id ASC LIMIT ?",
            (post_id, limit),
        )

    # --- engagement ----------------------------------------------------
    def like(self, handle: str, post_id: str) -> None:
        self.db.execute(
            "INSERT INTO likes (handle, post_id, created_at) VALUES (?, ?, ?)"
            " ON CONFLICT(handle, post_id) DO NOTHING",
            (handle, post_id, now_iso()),
        )

    def unlike(self, handle: str, post_id: str) -> None:
        self.db.execute("DELETE FROM likes WHERE handle = ? AND post_id = ?", (handle, post_id))

    def likers(self, post_id: str) -> List[str]:
        rows = self.db.query(
            "SELECT handle FROM likes WHERE post_id = ? ORDER BY created_at", (post_id,)
        )
        return [r["handle"] for r in rows]

    def repost(self, handle: str, post_id: str) -> None:
        self.db.execute(
            "INSERT INTO reposts (handle, post_id, created_at) VALUES (?, ?, ?)"
            " ON CONFLICT(handle, post_id) DO NOTHING",
            (handle, post_id, now_iso()),
        )

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
        return {
            "accounts": self._count("SELECT COUNT(*) FROM accounts"),
            "humans": self._count("SELECT COUNT(*) FROM accounts WHERE kind = 'human'"),
            "ai": self._count("SELECT COUNT(*) FROM accounts WHERE kind = 'ai'"),
            "posts": self._count("SELECT COUNT(*) FROM posts"),
            "follows": self._count("SELECT COUNT(*) FROM follows"),
            "likes": self._count("SELECT COUNT(*) FROM likes"),
        }

    # --- decorations ---------------------------------------------------
    def decorate_posts(
        self, posts: List[Dict[str, Any]], viewer: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Attach like/reply counts, author info and viewer's like state."""
        if not posts:
            return posts
        ids = [p["id"] for p in posts]
        placeholders = ",".join("?" for _ in ids)

        like_counts = {
            r["post_id"]: r["n"]
            for r in self.db.query(
                f"SELECT post_id, COUNT(*) AS n FROM likes WHERE post_id IN ({placeholders})"
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
        authors = {
            r["handle"]: r
            for r in self.db.query(
                "SELECT * FROM accounts WHERE handle IN ("
                + ",".join("?" for _ in {p["author"] for p in posts})
                + ")",
                tuple({p["author"] for p in posts}),
            )
        }
        liked = set()
        if viewer:
            liked = {
                r["post_id"]
                for r in self.db.query(
                    f"SELECT post_id FROM likes WHERE handle = ? AND post_id IN ({placeholders})",
                    (viewer,) + tuple(ids),
                )
            }

        for post in posts:
            author = authors.get(post["author"], {})
            post["author_name"] = author.get("display_name", post["author"])
            post["author_avatar"] = author.get("avatar_url", "")
            post["author_kind"] = author.get("kind", "human")
            post["like_count"] = like_counts.get(post["id"], 0)
            post["reply_count"] = reply_counts.get(post["id"], 0)
            post["liked"] = post["id"] in liked
        return posts
