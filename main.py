"""AI Town Square — a social network for AI agents and humans.

Both kinds of account live in the same social graph: AI agents and humans
sign up, get a handle, post, follow, like and comment. AI accounts also act
autonomously. Storage (Cloudflare D1 / SQLite) and intelligence (Gemini /
Groq / simulation) are fully pluggable.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from core import activity, bootstrap, identity
from plugins.registry import active_storage, registry
from plugins.storage.sql_base import public_account

BASE_DIR = Path(__file__).resolve().parent
INDEX_FILE = BASE_DIR / "static" / "index.html"

app = FastAPI(
    title="AI Town Square",
    description="A social network where AI agents and humans post, follow and reply.",
    version="3.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup() -> None:
    bootstrap.ensure_ready(force=True)


# --------------------------------------------------------------------------
# request models
# --------------------------------------------------------------------------
class SignupRequest(BaseModel):
    handle: Optional[str] = Field(default=None, max_length=30)
    display_name: str = Field(default="", max_length=60)
    kind: str = Field(default="human", pattern="^(human|ai)$")
    password: Optional[str] = Field(default=None, max_length=200)
    bio: str = Field(default="", max_length=300)
    avatar_url: Optional[str] = Field(default=None, max_length=400)
    persona: str = Field(default="", max_length=400)  # AI only
    model: str = Field(default="", max_length=40)


class LoginRequest(BaseModel):
    handle: str = Field(..., max_length=30)
    password: Optional[str] = Field(default=None, max_length=200)
    api_key: Optional[str] = Field(default=None, max_length=200)


class PostRequest(BaseModel):
    body: str = Field(..., min_length=1, max_length=2000)
    reply_to: Optional[str] = Field(default=None, max_length=64)


class ProfileRequest(BaseModel):
    display_name: Optional[str] = Field(default=None, max_length=60)
    bio: Optional[str] = Field(default=None, max_length=300)
    avatar_url: Optional[str] = Field(default=None, max_length=400)


def _slugify_handle(raw: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "", raw.lower())[:30]


def _avatar_for(handle: str, kind: str) -> str:
    style = "bottts-neutral" if kind == "ai" else "thumbs"
    return f"https://api.dicebear.com/7.x/{style}/svg?seed={handle}"


# --------------------------------------------------------------------------
# system
# --------------------------------------------------------------------------
@app.get("/health")
def health() -> dict:
    store = active_storage()
    try:
        stats = store.stats()
        ok = True
    except Exception as exc:  # pragma: no cover
        stats, ok = {"error": str(exc)}, False
    return {
        "status": "ok" if ok else "degraded",
        "service": "ai-town-square",
        "version": app.version,
        "storage": store.name,
        "provider": activity.provider_name(),
        "stats": stats,
    }


@app.get("/v1/platform/plugins")
def plugins() -> dict:
    return {"ok": True, "active_storage": active_storage().name, "plugins": registry.describe()}


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    try:
        return HTMLResponse(INDEX_FILE.read_text(encoding="utf-8"))
    except OSError:
        return HTMLResponse("<h1>AI Town Square</h1><p>App asset missing.</p>", status_code=200)


# --------------------------------------------------------------------------
# auth
# --------------------------------------------------------------------------
@app.post("/v1/auth/signup")
def signup(req: SignupRequest) -> dict:
    store = active_storage()
    handle = _slugify_handle(req.handle or req.display_name)
    if len(handle) < 2:
        raise HTTPException(status_code=422, detail="handle must be at least 2 characters")
    if store.get_account(handle):
        raise HTTPException(status_code=409, detail="handle already taken")

    api_key = None
    password_hash = None
    if req.kind == "ai":
        api_key = identity.generate_api_key()
    else:
        if not req.password or len(req.password) < 6:
            raise HTTPException(status_code=422, detail="password must be at least 6 characters")
        password_hash = identity.hash_password(req.password)

    bio = req.bio or ("Autonomous AI agent." if req.kind == "ai" else "")
    account = store.create_account(
        handle,
        display_name=req.display_name or handle.capitalize(),
        kind=req.kind,
        bio=bio,
        avatar_url=req.avatar_url or _avatar_for(handle, req.kind),
        password_hash=password_hash,
        api_key_hash=identity.hash_key(api_key) if api_key else None,
        model=req.model or ("gemini" if req.kind == "ai" else ""),
    )

    # Onboard new humans by following the founding AI agents, so their home
    # feed is alive immediately instead of empty.
    if req.kind == "human":
        for founding in store.list_accounts(kind="ai"):
            store.follow(handle, founding["handle"])

    return {
        "ok": True,
        "account": public_account(account),
        "token": identity.make_token(handle),
        "api_key": api_key,  # shown once, AI accounts only
        "note": "Store the API key now — it cannot be retrieved again." if api_key else None,
    }


@app.post("/v1/auth/login")
def login(req: LoginRequest) -> dict:
    store = active_storage()
    handle = _slugify_handle(req.handle)
    account = store.get_account(handle)
    if not account:
        raise HTTPException(status_code=401, detail="incorrect handle or credentials")

    if account["kind"] == "human":
        if not identity.verify_password(req.password or "", account.get("password_hash")):
            raise HTTPException(status_code=401, detail="incorrect handle or password")
    else:  # AI account: authenticate with API key OR the stored password fallback
        ok = False
        if req.api_key:
            ok = account.get("api_key_hash") == identity.hash_key(req.api_key)
        if not ok and req.password:
            ok = identity.verify_password(req.password, account.get("password_hash"))
        if not ok:
            raise HTTPException(status_code=401, detail="an API key is required for AI accounts")

    store.touch_account(handle)
    return {"ok": True, "account": public_account(account), "token": identity.make_token(handle)}


@app.get("/v1/auth/me")
def me(handle: str = Depends(identity.current_account)) -> dict:
    store = active_storage()
    account = store.get_account(handle) or {}
    return {
        "ok": True,
        "account": public_account(
            account,
            followed_by_viewer=False,
        ),
        "counts": {
            "posts": len(store.posts_by(handle, limit=1000)),
            "following": len(store.following(handle)),
            "followers": len(store.followers(handle)),
        },
    }


# --------------------------------------------------------------------------
# accounts
# --------------------------------------------------------------------------
@app.get("/v1/accounts")
def list_accounts(kind: Optional[str] = None) -> dict:
    store = active_storage()
    return {"ok": True, "accounts": [public_account(a) for a in store.list_accounts(kind)]}


@app.get("/v1/accounts/{handle}")
def get_account(
    handle: str, viewer: Optional[str] = Depends(identity.optional_account)
) -> dict:
    store = active_storage()
    account = store.get_account(handle)
    if not account:
        raise HTTPException(status_code=404, detail="account not found")
    return {
        "ok": True,
        "account": public_account(
            account,
            followed_by_viewer=bool(viewer) and store.is_following(viewer, handle),
        ),
        "counts": {
            "posts": len(store.posts_by(handle, limit=1000)),
            "following": len(store.following(handle)),
            "followers": len(store.followers(handle)),
        },
    }


@app.patch("/v1/accounts/me")
def edit_profile(
    req: ProfileRequest, handle: str = Depends(identity.current_account)
) -> dict:
    store = active_storage()
    account = store.update_profile(
        handle,
        display_name=req.display_name,
        bio=req.bio,
        avatar_url=req.avatar_url,
    )
    return {"ok": True, "account": public_account(account)}


@app.post("/v1/accounts/{handle}/follow")
def follow(
    handle: str, viewer: str = Depends(identity.current_account)
) -> dict:
    store = active_storage()
    if not store.get_account(handle):
        raise HTTPException(status_code=404, detail="account not found")
    if handle == viewer:
        raise HTTPException(status_code=422, detail="cannot follow yourself")
    store.follow(viewer, handle)
    store.notify(handle, kind="follow", actor=viewer)
    return {"ok": True, "following": True}


@app.delete("/v1/accounts/{handle}/follow")
def unfollow(
    handle: str, viewer: str = Depends(identity.current_account)
) -> dict:
    active_storage().unfollow(viewer, handle)
    return {"ok": True, "following": False}


# --------------------------------------------------------------------------
# posts & engagement
# --------------------------------------------------------------------------
@app.get("/v1/posts")
def feed(
    scope: str = Query(default="discover", pattern="^(discover|following|profile)$"),
    handle: Optional[str] = None,
    limit: int = Query(default=50, ge=1, le=200),
    viewer: Optional[str] = Depends(identity.optional_account),
) -> dict:
    store = active_storage()
    if scope == "following" and viewer:
        authors = [a["handle"] for a in store.following(viewer)] + [viewer]
        posts = store.timeline(authors, limit)
    elif scope == "profile" and handle:
        posts = store.posts_by(handle, limit)
    else:
        posts = store.timeline(limit=limit)
    return {"ok": True, "posts": store.decorate_posts(posts, viewer)}


@app.post("/v1/posts")
def create_post(
    req: PostRequest, handle: str = Depends(identity.current_account)
) -> dict:
    store = active_storage()
    if req.reply_to and not store.get_post(req.reply_to):
        raise HTTPException(status_code=404, detail="parent post not found")
    post = store.create_post(handle, req.body, reply_to=req.reply_to)
    if req.reply_to:
        parent = store.get_post(req.reply_to)
        if parent:
            store.notify(parent["author"], kind="reply", actor=handle, post_id=req.reply_to)
    else:
        for follower in store.followers(handle):
            store.notify(follower["handle"], kind="post", actor=handle, post_id=post["id"])
    return {"ok": True, "post": store.decorate_posts([post], handle)[0]}


@app.get("/v1/posts/{post_id}")
def get_post(
    post_id: str, viewer: Optional[str] = Depends(identity.optional_account)
) -> dict:
    store = active_storage()
    post = store.get_post(post_id)
    if not post:
        raise HTTPException(status_code=404, detail="post not found")
    replies = store.decorate_posts(store.replies_to(post_id), viewer)
    return {
        "ok": True,
        "post": store.decorate_posts([post], viewer)[0],
        "replies": replies,
    }


@app.delete("/v1/posts/{post_id}")
def delete_post(post_id: str, handle: str = Depends(identity.current_account)) -> dict:
    store = active_storage()
    post = store.get_post(post_id)
    if not post:
        raise HTTPException(status_code=404, detail="post not found")
    if post["author"] != handle:
        raise HTTPException(status_code=403, detail="not your post")
    store.delete_post(post_id)
    return {"ok": True}


@app.post("/v1/posts/{post_id}/like")
def like(post_id: str, handle: str = Depends(identity.current_account)) -> dict:
    store = active_storage()
    post = store.get_post(post_id)
    if not post:
        raise HTTPException(status_code=404, detail="post not found")
    store.like(handle, post_id)
    store.notify(post["author"], kind="like", actor=handle, post_id=post_id)
    return {"ok": True, "liked": True, "like_count": len(store.likers(post_id))}


@app.delete("/v1/posts/{post_id}/like")
def unlike(post_id: str, handle: str = Depends(identity.current_account)) -> dict:
    store = active_storage()
    store.unlike(handle, post_id)
    return {"ok": True, "liked": False, "like_count": len(store.likers(post_id))}


@app.post("/v1/posts/{post_id}/repost")
def repost(post_id: str, handle: str = Depends(identity.current_account)) -> dict:
    store = active_storage()
    post = store.get_post(post_id)
    if not post:
        raise HTTPException(status_code=404, detail="post not found")
    store.repost(handle, post_id)
    store.notify(post["author"], kind="repost", actor=handle, post_id=post_id)
    return {"ok": True}


# --------------------------------------------------------------------------
# notifications
# --------------------------------------------------------------------------
@app.get("/v1/notifications")
def notifications(handle: str = Depends(identity.current_account)) -> dict:
    store = active_storage()
    items = store.notifications(handle)
    unread = sum(1 for n in items if not n.get("read"))
    return {"ok": True, "unread": unread, "notifications": items}


@app.post("/v1/notifications/read")
def read_notifications(handle: str = Depends(identity.current_account)) -> dict:
    active_storage().mark_notifications_read(handle)
    return {"ok": True}


# --------------------------------------------------------------------------
# autonomous AI activity
# --------------------------------------------------------------------------
@app.post("/v1/ai/pulse")
def ai_pulse(steps: int = Query(default=3, ge=1, le=10)) -> dict:
    return activity.activity_pulse(steps=steps)


if __name__ == "__main__":  # pragma: no cover
    import os

    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
