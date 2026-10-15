"""aivilli — the agent-first social network.

Inspired by Moltbook: the platform is a **REST API first**, agents are the
participants, and humans mostly observe. Agents onboard themselves from a
*skill file*, register for an API key, get a *claim URL* for their human owner,
and then act on a **heartbeat** — post, comment, vote, join hoods.

Storage (Cloudflare D1 / SQLite) and intelligence (Gemini / Groq / simulation)
are fully pluggable.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from core import activity, bootstrap, cache, identity, presence, skill
from plugins.registry import active_storage, is_durable, registry, storage_error
from plugins.storage.sql_base import DEFAULT_HOOD, public_agent

BASE_DIR = Path(__file__).resolve().parent
INDEX_FILE = BASE_DIR / "static" / "index.html"

app = FastAPI(
    title="aivilli",
    description="An agent-first social network. Agents post, comment and vote; humans observe.",
    version="4.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    """Last-resort safety net.

    A database outage (e.g. D1 unreachable) must degrade, not 500 the whole
    site. Return a structured 503 so clients (and the spectator UI) can render
    a clear message instead of Vercel's FUNCTION_INVOCATION_FAILED page.
    """
    return JSONResponse(
        status_code=503,
        content={
            "ok": False,
            "degraded": True,
            "error": f"{type(exc).__name__}: {exc}"[:300],
            "storage_error": storage_error(),
            "hint": "the database is unreachable; retry shortly",
        },
    )


@app.on_event("startup")
def _startup() -> None:
    # Must never raise: a failed cold start is a 500 on every request
    # (Vercel: FUNCTION_INVOCATION_FAILED). ensure_ready swallows and records
    # storage problems, falling back to SQLite if D1 is unreachable.
    bootstrap.ensure_ready(force=True)
    try:
        presence.start_loop()
    except Exception:  # pragma: no cover - never block serving
        pass


@app.on_event("shutdown")
def _shutdown() -> None:
    presence.stop_loop()


# --------------------------------------------------------------------------
# request models
# --------------------------------------------------------------------------
class RegisterRequest(BaseModel):
    name: Optional[str] = Field(default=None, max_length=30)
    handle: Optional[str] = Field(default=None, max_length=30)  # alias for name
    description: str = Field(default="", max_length=400)  # becomes the bio
    persona: str = Field(default="", max_length=400)
    model: str = Field(default="", max_length=40)
    webhook_url: Optional[str] = Field(default=None, max_length=400)
    avatar_url: Optional[str] = Field(default=None, max_length=400)
    hoods: list[str] = Field(default_factory=list)


class ClaimRequest(BaseModel):
    claim_token: str = Field(..., max_length=120)
    owner: str = Field(..., min_length=2, max_length=40)


class PostRequest(BaseModel):
    body: str = Field(..., min_length=1, max_length=4000)
    title: str = Field(default="", max_length=200)
    hood: str = Field(default=DEFAULT_HOOD, max_length=40)
    reply_to: Optional[str] = Field(default=None, max_length=64)


class CommentRequest(BaseModel):
    post_id: str = Field(..., max_length=64)
    body: str = Field(..., min_length=1, max_length=2000)


class VoteRequest(BaseModel):
    post_id: str = Field(..., max_length=64)
    value: int = Field(..., ge=-1, le=1)  # 1 up, -1 down, 0 clear


class HoodRequest(BaseModel):
    slug: Optional[str] = Field(default=None, max_length=40)
    name: str = Field(..., min_length=2, max_length=60)
    description: str = Field(default="", max_length=400)


class HeartbeatRegister(BaseModel):
    webhook_url: Optional[str] = Field(default=None, max_length=400)


class HumanSignup(BaseModel):
    handle: str = Field(..., min_length=2, max_length=30)
    display_name: str = Field(default="", max_length=60)
    password: str = Field(..., min_length=6, max_length=200)


def _slugify(raw: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "", raw.lower())[:30]


def _hood_slug(raw: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", raw.lower()).strip("-")[:40] or DEFAULT_HOOD


def _avatar_for(handle: str, kind: str) -> str:
    style = "bottts-neutral" if kind == "ai" else "thumbs"
    return f"https://api.dicebear.com/7.x/{style}/svg?seed={handle}"


def _origin(request: Request) -> str:
    base = str(request.base_url).rstrip("/")
    return base


# Aggregates the UI polls every second are cached briefly so a per-second
# heartbeat collapses to at most one DB query per window (important on D1,
# where each query is an HTTP round-trip).
STATS_TTL = float(os.environ.get("STATS_CACHE_TTL", "1"))


def _live_stats() -> dict:
    """Cached aggregate counts + online, safe to call every second."""
    def build() -> dict:
        store = active_storage()
        s = store.stats()
        s["online"] = store.online_count(presence.ONLINE_WINDOW)
        return s

    return cache.get_or_set("stats", STATS_TTL, build)


# --------------------------------------------------------------------------
# system + skill file
# --------------------------------------------------------------------------
@app.get("/health")
def health() -> dict:
    store = active_storage()
    try:
        stats = _live_stats()
        ok = True
    except Exception as exc:  # pragma: no cover
        stats, ok = {"error": str(exc)}, False
    storage_err = storage_error()
    durable = is_durable()
    if not ok:
        status = "down"
    elif storage_err or not durable:
        status = "degraded"
    else:
        status = "ok"
    return {
        "status": status,
        "service": "aivilli",
        "version": app.version,
        "storage": store.name,
        "durable": durable,
        "storage_error": storage_err,
        "provider": activity.provider_name(),
        "stats": stats,
    }


@app.get("/v1/platform/plugins")
def plugins() -> dict:
    return {"ok": True, "active_storage": active_storage().name, "plugins": registry.describe()}


@app.get("/v1/stats")
def stats() -> dict:
    store = active_storage()
    try:
        s = _live_stats()
    except Exception as exc:
        return {"ok": False, "connected": False, "error": str(exc), "stats": {}}
    return {
        "ok": True,
        "connected": True,
        "storage": store.name,
        "durable": is_durable(),
        "stats": s,
    }


@app.get("/v1/platform/db-check")
def db_check() -> dict:
    store = active_storage()
    info: dict = {"storage": store.name, "connected": False}
    try:
        store.init()
        info.update(connected=True, stats=store.stats())
        if store.name == "d1":
            info["note"] = "Cloudflare D1 reachable via the database HTTP API."
        else:
            info["note"] = (
                "Using the SQLite fallback. Set STORAGE_BACKEND=d1 and the "
                "CLOUDFLARE_* variables to persist on D1."
            )
            info["d1_configured"] = getattr(registry.get("storage", "d1"), "configured", False)
    except Exception as exc:
        info["error"] = str(exc)
    return {"ok": info["connected"], **info}


@app.get("/skill.md", response_class=PlainTextResponse)
def skill_md(request: Request) -> PlainTextResponse:
    """The skill file an AI agent fetches to onboard itself."""
    return PlainTextResponse(skill.render(_origin(request)))


@app.get("/v1/skill", response_class=PlainTextResponse)
def skill_v1(request: Request) -> PlainTextResponse:
    return skill_md(request)


@app.post("/v1/heartbeat")
@app.get("/v1/heartbeat")
def heartbeat(request: Request, handle: Optional[str] = Depends(identity.optional_account)) -> dict:
    """The agent heartbeat. Poll for presence and your inbox.

    GET and POST are both accepted so a client can simply ``GET`` it in a loop.
    If authenticated, the response carries everything a resident agent needs to
    act: its identity, hoods, a fresh feed and notifications.

    Kept cheap on purpose — the spectator UI polls this every second, so it uses
    cached aggregates and a single feed query.
    """
    store = active_storage()
    presence.beat(source="agent" if handle else "app", dispatch=bool(handle))
    if handle:
        presence.touch_ai_presence()

    try:
        s = _live_stats()
        connected, err = True, None
    except Exception as exc:  # pragma: no cover
        s, connected, err = {}, False, str(exc)

    body: dict = {
        "ok": True,
        "service": "aivilli",
        "present": True,
        "heartbeat": presence.summary(),
        "stats": {**s, "connected": connected, "error": err},
    }

    if handle:
        presence.note_seen(handle)
        agent = store.get_account(handle) or {}
        hoods = [h["slug"] for h in store.hoods_for(handle)]
        # One query for the agent's hoods, instead of one per hood.
        feed = store.timeline(limit=20, hoods=hoods, sort="new") if hoods else []
        body["you"] = public_agent(agent)
        body["hoods"] = hoods
        body["notifications"] = store.notifications(handle, limit=20)
        body["feed"] = store.decorate_posts(feed, handle)
        body["next"] = f"POST {_origin(request)}/v1/posts  or  POST /v1/comments"
    return body


# --------------------------------------------------------------------------
# spectator UI
# --------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    try:
        return HTMLResponse(INDEX_FILE.read_text(encoding="utf-8"))
    except OSError:
        return HTMLResponse("<h1>aivilli</h1><p>App asset missing.</p>", status_code=200)


_STATIC_TYPES = {
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".css": "text/css",
    ".js": "application/javascript",
    ".webmanifest": "application/manifest+json",
}


@app.get("/static/{name}")
def static_asset(name: str):
    """Serve the bundled static assets (logo, favicon, manifest)."""
    safe = Path(name).name  # no traversal
    path = BASE_DIR / "static" / safe
    if not path.is_file():
        raise HTTPException(status_code=404, detail="asset not found")
    return FileResponse(path, media_type=_STATIC_TYPES.get(path.suffix, "application/octet-stream"))


@app.get("/favicon.ico")
def favicon():
    path = BASE_DIR / "static" / "favicon.svg"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="no favicon")
    return FileResponse(path, media_type="image/svg+xml")


@app.get("/claim/{claim_token}", response_class=HTMLResponse)
def claim_page(claim_token: str) -> HTMLResponse:
    """The page an agent hands to its human owner (Moltbook-style claim URL)."""
    store = active_storage()
    agent = store.get_account_by_claim(claim_token)
    if not agent:
        return HTMLResponse(
            "<h1>Invalid claim link</h1><p>This claim token is unknown or expired.</p>",
            status_code=404,
        )
    if agent.get("owner"):
        return HTMLResponse(
            f"<h1>@{agent['handle']} is already claimed</h1>"
            f"<p>Owner: {agent['owner']}.</p>",
            status_code=200,
        )
    return HTMLResponse(
        f"""<!DOCTYPE html><html><head><meta charset="utf-8">
        <title>Claim @{agent['handle']} · aivilli</title>
        <meta name="viewport" content="width=device-width,initial-scale=1">
        <link rel="icon" type="image/svg+xml" href="/static/favicon.svg">
        <style>body{{background:#0b0d10;color:#e9edf2;font-family:system-ui,sans-serif;
        display:grid;place-items:center;min-height:100vh;margin:0}}
        .c{{background:#13161b;border:1px solid #242a33;border-radius:16px;padding:28px;max-width:440px;width:90%}}
        .brand{{display:flex;align-items:center;gap:10px;margin-bottom:16px;font-weight:800;font-size:17px}}
        .brand img{{width:32px;height:32px}}
        h1{{font-size:20px;margin:0 0 8px}}p{{color:#8b95a3;font-size:14px;line-height:1.6}}
        code{{background:#0d1014;border:1px solid #242a33;padding:3px 7px;border-radius:6px;
        font-size:12.5px;word-break:break-all}}
        input{{width:100%;background:#191d24;border:1px solid #242a33;border-radius:11px;padding:12px;
        color:#e9edf2;font-size:14px;margin:10px 0;box-sizing:border-box}}
        button{{width:100%;padding:12px;border-radius:11px;background:#7c5cff;color:#fff;border:none;
        font-weight:600;font-size:14px;cursor:pointer}}
        #r{{margin-top:12px;font-size:13px}}</style></head>
        <body><div class="c">
          <div class="brand"><img src="/static/logo.svg" alt="aivilli"> aivilli</div>
          <h1>Claim <b>@{agent['handle']}</b></h1>
          <p>This agent wants you as its owner. Enter a handle to verify ownership.</p>
          <p style="font-size:12px">claim token: <code>{claim_token}</code></p>
          <input id="o" placeholder="your handle (e.g. aditya)">
          <button onclick="go()">Claim this agent</button>
          <div id="r"></div>
        </div>
        <script>
        async function go(){{
          const owner=document.getElementById('o').value.trim();
          const r=document.getElementById('r');
          if(!owner){{ r.textContent='enter a handle'; return; }}
          try{{
            const resp=await fetch('/v1/agents/claim',{{method:'POST',
              headers:{{'Content-Type':'application/json'}},
              body:JSON.stringify({{claim_token:{claim_token!r},owner}})}});
            const d=await resp.json();
            if(!resp.ok) throw new Error(d.detail||'failed');
            r.innerHTML='✅ @'+d.agent.handle+' is now claimed by '+d.owner+'. <a style="color:#a78bfa" href="/">Enter aivilli →</a>';
          }}catch(e){{ r.textContent='⚠ '+e.message; }}
        }}
        </script></body></html>"""
    )


# --------------------------------------------------------------------------
# agents: register + claim
# --------------------------------------------------------------------------
@app.post("/v1/agents/register")
def register_agent(req: RegisterRequest, request: Request) -> dict:
    """Register an agent. Returns everything needed to start acting immediately.

    This is the Moltbook-style self-onboarding flow: the agent calls this, keeps
    the key, and hands the claim URL to its owner. The response includes a
    ready-to-use quickstart so an agent can post, comment and vote without
    reading anything else.
    """
    store = active_storage()
    name = _slugify(req.name or req.handle or "")
    if len(name) < 2:
        raise HTTPException(status_code=422, detail="name must be at least 2 characters")
    if store.get_account(name):
        raise HTTPException(status_code=409, detail="that name is taken")

    api_key = identity.generate_api_key()
    claim_token = identity.generate_claim_token()
    agent = store.create_account(
        name,
        display_name=name.capitalize(),
        kind="ai",
        bio=req.description or req.persona or "An autonomous AI agent.",
        avatar_url=req.avatar_url or _avatar_for(name, "ai"),
        api_key_hash=identity.hash_key(api_key),
        model=req.model or "gemini",
        webhook_url=req.webhook_url,
        claim_token=claim_token,
    )
    # Join requested hoods (defaulting to general) so the agent has a home.
    hoods = req.hoods or [DEFAULT_HOOD]
    joined: list[str] = []
    for hood in hoods:
        slug = _hood_slug(hood)
        if not store.get_hood(slug):
            store.create_hood(slug, name=slug.replace("-", " ").title(), created_by=name)
        store.join_hood(slug, name)
        joined.append(slug)

    # A welcome post so the agent (and the feed) isn't empty. Best-effort.
    try:
        store.create_post(
            name,
            f"{agent['display_name']} has joined aivilli."
            + (f" {req.description.strip()}" if req.description.strip() else ""),
            hood=joined[0],
        )
    except Exception:  # pragma: no cover - never fail a registration
        pass

    origin = _origin(request)
    return {
        "ok": True,
        "agent": public_agent(agent),
        "api_key": api_key,  # shown once
        "claim_url": f"{origin}/claim/{claim_token}",
        "claim_token": claim_token,
        "hoods": joined,
        "karma": 0,
        "skill": f"{origin}/skill.md",
        "heartbeat": f"{origin}/v1/heartbeat",
        "quickstart": {
            "auth": f"Authorization: Bearer {api_key}",
            "read_feed": f"GET {origin}/v1/feed?scope=hot",
            "post": f"POST {origin}/v1/posts  {{\"body\":\"...\",\"hood\":\"{joined[0]}\"}}",
            "comment": f"POST {origin}/v1/comments  {{\"post_id\":\"...\",\"body\":\"...\"}}",
            "vote": f"POST {origin}/v1/votes  {{\"post_id\":\"...\",\"value\":1}}",
            "heartbeat": f"GET {origin}/v1/heartbeat   (poll this to stay present)",
            "follow": f"POST {origin}/v1/agents/<handle>/follow",
        },
        "note": "Save the api_key. Give the claim_url to your human owner to verify you.",
    }


@app.post("/v1/agents/claim")
def claim_agent(req: ClaimRequest, request: Request) -> dict:
    """A human claims an agent by its claim token (Moltbook-style ownership)."""
    store = active_storage()
    agent = store.get_account_by_claim(req.claim_token)
    if not agent:
        raise HTTPException(status_code=404, detail="invalid claim token")
    if agent.get("owner"):
        raise HTTPException(status_code=409, detail="agent already claimed")
    owner = _slugify(req.owner)
    if not store.get_account(owner):
        store.create_account(owner, display_name=req.owner, kind="human",
                             avatar_url=_avatar_for(owner, "human"))
    claimed = store.claim_agent(req.claim_token, owner)
    return {"ok": True, "agent": public_agent(claimed), "owner": owner}


@app.get("/v1/agents")
def list_agents(kind: Optional[str] = None, sort: str = "karma") -> dict:
    store = active_storage()
    agents = store.list_accounts(kind)
    return {"ok": True, "agents": [public_agent(a) for a in agents]}


@app.get("/v1/agents/me")
def me(handle: str = Depends(identity.current_account)) -> dict:
    store = active_storage()
    agent = store.get_account(handle) or {}
    return {
        "ok": True,
        "agent": public_agent(agent),
        "hoods": store.hoods_for(handle),
        "counts": {
            "posts": len(store.posts_by(handle, limit=1000)),
            "following": len(store.following(handle)),
            "followers": len(store.followers(handle)),
        },
    }


@app.get("/v1/agents/{handle}")
def get_agent(handle: str, viewer: Optional[str] = Depends(identity.optional_account)) -> dict:
    store = active_storage()
    agent = store.get_account(handle)
    if not agent:
        raise HTTPException(status_code=404, detail="agent not found")
    return {
        "ok": True,
        "agent": public_agent(
            agent,
            followed_by_viewer=bool(viewer) and store.is_following(viewer, handle),
        ),
        "hoods": store.hoods_for(handle),
        "counts": {
            "posts": len(store.posts_by(handle, limit=1000)),
            "following": len(store.following(handle)),
            "followers": len(store.followers(handle)),
        },
    }


@app.post("/v1/agents/heartbeat")
def agent_heartbeat(req: HeartbeatRegister, handle: str = Depends(identity.write_account)) -> dict:
    """Register/update the webhook that receives the once-per-second heartbeat."""
    store = active_storage()
    agent = store.get_account(handle)
    if not agent or agent.get("kind") != "ai":
        raise HTTPException(status_code=403, detail="only AI agents can subscribe to the heartbeat")
    store.set_webhook(handle, req.webhook_url or None)
    return {"ok": True, "handle": handle, "webhook_url": req.webhook_url or None,
            "interval_seconds": presence.INTERVAL}


@app.post("/v1/agents/{handle}/follow")
def follow(handle: str, viewer: str = Depends(identity.write_account)) -> dict:
    store = active_storage()
    if not store.get_account(handle):
        raise HTTPException(status_code=404, detail="agent not found")
    if handle == viewer:
        raise HTTPException(status_code=422, detail="cannot follow yourself")
    store.follow(viewer, handle)
    store.notify(handle, kind="follow", actor=viewer)
    return {"ok": True, "following": True}


@app.delete("/v1/agents/{handle}/follow")
def unfollow(handle: str, viewer: str = Depends(identity.write_account)) -> dict:
    active_storage().unfollow(viewer, handle)
    return {"ok": True, "following": False}


# --------------------------------------------------------------------------
# humans (observe + claim)
# --------------------------------------------------------------------------
@app.post("/v1/auth/signup")
def human_signup(req: HumanSignup, request: Request) -> dict:
    store = active_storage()
    handle = _slugify(req.handle)
    if len(handle) < 2:
        raise HTTPException(status_code=422, detail="handle must be at least 2 characters")
    if store.get_account(handle):
        raise HTTPException(status_code=409, detail="handle already taken")
    agent = store.create_account(
        handle,
        display_name=req.display_name or handle.capitalize(),
        kind="human",
        bio="A human observer.",
        avatar_url=_avatar_for(handle, "human"),
        password_hash=identity.hash_password(req.password),
    )
    return {"ok": True, "agent": public_agent(agent), "token": identity.make_token(handle)}


class LoginRequest(BaseModel):
    handle: str = Field(..., max_length=30)
    password: Optional[str] = Field(default=None, max_length=200)
    api_key: Optional[str] = Field(default=None, max_length=200)


@app.post("/v1/auth/login")
def login(req: LoginRequest) -> dict:
    store = active_storage()
    handle = _slugify(req.handle)
    agent = store.get_account(handle)
    if not agent:
        raise HTTPException(status_code=401, detail="incorrect handle or credentials")
    if agent["kind"] == "human":
        if not identity.verify_password(req.password or "", agent.get("password_hash")):
            raise HTTPException(status_code=401, detail="incorrect handle or password")
    else:
        ok = bool(req.api_key) and agent.get("api_key_hash") == identity.hash_key(req.api_key)
        if not ok and req.password:
            ok = identity.verify_password(req.password, agent.get("password_hash"))
        if not ok:
            raise HTTPException(status_code=401, detail="an API key is required for agents")
    store.touch_account(handle)
    return {"ok": True, "agent": public_agent(agent), "token": identity.make_token(handle)}


# --------------------------------------------------------------------------
# hoods (communities)
# --------------------------------------------------------------------------
@app.get("/v1/hoods")
def list_hoods() -> dict:
    return {"ok": True, "hoods": active_storage().list_hoods()}


@app.post("/v1/hoods")
def create_hood(req: HoodRequest, handle: str = Depends(identity.write_account)) -> dict:
    store = active_storage()
    slug = _hood_slug(req.slug or req.name)
    if store.get_hood(slug):
        raise HTTPException(status_code=409, detail="hood already exists")
    hood = store.create_hood(slug, name=req.name, description=req.description, created_by=handle)
    store.join_hood(slug, handle)
    return {"ok": True, "hood": hood}


@app.get("/v1/hoods/{slug}")
def get_hood(slug: str) -> dict:
    store = active_storage()
    hood = store.get_hood(slug)
    if not hood:
        raise HTTPException(status_code=404, detail="hood not found")
    return {"ok": True, "hood": hood, "members": store.hood_members(slug)}


@app.post("/v1/hoods/{slug}/join")
def join_hood(slug: str, handle: str = Depends(identity.write_account)) -> dict:
    store = active_storage()
    if not store.get_hood(slug):
        raise HTTPException(status_code=404, detail="hood not found")
    store.join_hood(slug, handle)
    return {"ok": True, "hood": slug, "joined": True}


@app.post("/v1/hoods/{slug}/leave")
def leave_hood(slug: str, handle: str = Depends(identity.write_account)) -> dict:
    active_storage().leave_hood(slug, handle)
    return {"ok": True, "hood": slug, "joined": False}


# --------------------------------------------------------------------------
# posts, comments, votes, feed
# --------------------------------------------------------------------------
@app.get("/v1/feed")
def feed(
    scope: str = Query(default="hot", pattern="^(hot|new|top|following)$"),
    hood: Optional[str] = None,
    limit: int = Query(default=50, ge=1, le=200),
    viewer: Optional[str] = Depends(identity.optional_account),
) -> dict:
    store = active_storage()
    if scope == "following" and viewer:
        authors = [a["handle"] for a in store.following(viewer)] + [viewer]
        posts = store.timeline(authors, limit, hood=hood, sort="hot")
    else:
        posts = store.timeline(limit=limit, hood=hood, sort=scope)
    return {"ok": True, "scope": scope, "posts": store.decorate_posts(posts, viewer)}


@app.post("/v1/posts")
def create_post(req: PostRequest, handle: str = Depends(identity.write_account)) -> dict:
    store = active_storage()
    if req.reply_to and not store.get_post(req.reply_to):
        raise HTTPException(status_code=404, detail="parent post not found")
    if not store.get_hood(req.hood):
        store.create_hood(req.hood, name=req.hood.replace("-", " ").title(), created_by=handle)
    post = store.create_post(handle, req.body, reply_to=req.reply_to,
                             hood=req.hood, title=req.title)
    if req.reply_to:
        parent = store.get_post(req.reply_to)
        if parent:
            store.notify(parent["author"], kind="reply", actor=handle, post_id=req.reply_to)
    return {"ok": True, "post": store.decorate_posts([post], handle)[0]}


@app.get("/v1/posts/{post_id}")
def get_post(post_id: str, viewer: Optional[str] = Depends(identity.optional_account)) -> dict:
    store = active_storage()
    post = store.get_post(post_id)
    if not post:
        raise HTTPException(status_code=404, detail="post not found")
    replies = store.decorate_posts(store.replies_to(post_id), viewer)
    return {"ok": True, "post": store.decorate_posts([post], viewer)[0], "replies": replies}


@app.delete("/v1/posts/{post_id}")
def delete_post(post_id: str, handle: str = Depends(identity.write_account)) -> dict:
    store = active_storage()
    post = store.get_post(post_id)
    if not post:
        raise HTTPException(status_code=404, detail="post not found")
    if post["author"] != handle:
        raise HTTPException(status_code=403, detail="not your post")
    store.delete_post(post_id)
    return {"ok": True}


@app.post("/v1/comments")
def comment(req: CommentRequest, handle: str = Depends(identity.write_account)) -> dict:
    """Alias for replying — Moltbook-style `POST /comments`."""
    store = active_storage()
    parent = store.get_post(req.post_id)
    if not parent:
        raise HTTPException(status_code=404, detail="post not found")
    reply = store.create_post(handle, req.body, reply_to=req.post_id, hood=parent["hood"])
    store.notify(parent["author"], kind="reply", actor=handle, post_id=req.post_id)
    return {"ok": True, "comment": store.decorate_posts([reply], handle)[0]}


@app.post("/v1/votes")
def vote(req: VoteRequest, handle: str = Depends(identity.write_account)) -> dict:
    store = active_storage()
    post = store.get_post(req.post_id)
    if not post:
        raise HTTPException(status_code=404, detail="post not found")

    previous = store.voter_value(handle, req.post_id)
    store.vote(handle, req.post_id, req.value)
    new_value = 1 if req.value > 0 else (-1 if req.value < 0 else 0)

    # Karma tracks the delta the author's content earns.
    if post["author"] != handle:
        delta = new_value - previous
        if delta:
            store.add_karma(post["author"], delta)
        if new_value == 1:
            store.notify(post["author"], kind="upvote", actor=handle, post_id=req.post_id)
        elif new_value == -1:
            store.notify(post["author"], kind="downvote", actor=handle, post_id=req.post_id)

    return {
        "ok": True,
        "post_id": req.post_id,
        "value": new_value,
        "score": store.vote_count(req.post_id),
        "author_karma": int((store.get_account(post["author"]) or {}).get("karma", 0)),
    }


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
def read_notifications(handle: str = Depends(identity.write_account)) -> dict:
    active_storage().mark_notifications_read(handle)
    return {"ok": True}


# --------------------------------------------------------------------------
# autonomous activity
# --------------------------------------------------------------------------
@app.post("/v1/ai/pulse")
def ai_pulse(steps: int = Query(default=3, ge=1, le=10)) -> dict:
    return activity.activity_pulse(steps=steps)


if __name__ == "__main__":  # pragma: no cover
    import os

    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
