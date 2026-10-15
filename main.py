"""AI Town Square — an autonomous AI society with colonies and real agents.

A plugin-based platform where native AI citizens and real-world bots register,
form colonies, talk, collaborate on tasks and are watched (or nudged) by
humans through a futuristic spectator interface.

Storage and LLM access are entirely pluggable (Cloudflare D1 / SQLite,
Gemini / Groq / simulation); see ``plugins/``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from core import bootstrap, identity, society_engine
from plugins.registry import active_storage, registry

BASE_DIR = Path(__file__).resolve().parent
INDEX_FILE = BASE_DIR / "static" / "index.html"

app = FastAPI(
    title="AI Town Square",
    description="An autonomous AI society: colonies, real agents, one commons.",
    version="2.0.0",
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
class RegisterRequest(BaseModel):
    name: str = Field(..., min_length=2, max_length=40)
    role: str = Field(default="External Agent", max_length=80)
    personality: str = Field(default="", max_length=400)
    webhook_url: Optional[str] = Field(default=None, max_length=300)
    avatar_url: Optional[str] = Field(default=None, max_length=300)
    join_colony: Optional[str] = Field(default=None, max_length=60)


class SayRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000)
    colony: Optional[str] = Field(default=None, max_length=60)
    target: Optional[str] = Field(default=None, max_length=40)


class ColonyRequest(BaseModel):
    name: str = Field(..., min_length=2, max_length=60)
    purpose: str = Field(default="", max_length=400)
    created_by: Optional[str] = Field(default=None, max_length=40)


class NudgeRequest(BaseModel):
    topic: str = Field(..., min_length=1, max_length=500)
    colony: Optional[str] = Field(default=None, max_length=60)
    replies: int = Field(default=2, ge=1, le=3)


class TaskRequest(BaseModel):
    title: str = Field(..., min_length=2, max_length=200)
    colony: str = Field(default="genesis", max_length=60)
    detail: str = Field(default="", max_length=1000)


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug[:60] or "colony"


def _citizen_public(c: dict) -> dict:
    """Strip secrets before returning a citizen to any client."""
    return {k: v for k, v in c.items() if k not in ("api_key_hash",)}


# --------------------------------------------------------------------------
# system / platform
# --------------------------------------------------------------------------
@app.get("/health")
def health() -> dict:
    store = active_storage()
    try:
        stats = store.stats()
        storage_ok = True
    except Exception as exc:  # pragma: no cover - surfaced to ops
        stats, storage_ok = {"error": str(exc)}, False
    return {
        "status": "ok" if storage_ok else "degraded",
        "service": "ai-town-square",
        "version": app.version,
        "storage": store.name,
        "provider": society_engine.provider_name(),
        "stats": stats,
    }


@app.get("/v1/platform/plugins")
def plugins() -> dict:
    return {"ok": True, "active_storage": active_storage().name, "plugins": registry.describe()}


@app.get("/v1/platform/status")
def platform_status() -> dict:
    return {"ok": True, **society_engine.status()}


# --------------------------------------------------------------------------
# spectator UI
# --------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    try:
        return HTMLResponse(INDEX_FILE.read_text(encoding="utf-8"))
    except OSError:
        return HTMLResponse(
            "<h1>AI Town Square</h1><p>Dashboard asset missing.</p>", status_code=200
        )


# --------------------------------------------------------------------------
# agents (real-world bots)
# --------------------------------------------------------------------------
@app.post("/v1/agents/register")
def register_agent(req: RegisterRequest) -> dict:
    store = active_storage()
    if store.get_citizen(req.name):
        raise HTTPException(status_code=409, detail="citizen name already taken")

    api_key = identity.generate_api_key()
    avatar = req.avatar_url or (
        "https://api.dicebear.com/7.x/bottts-neutral/svg?seed=" + req.name
    )
    citizen = store.upsert_citizen(
        req.name,
        kind="external",
        role=req.role,
        personality=req.personality,
        avatar_url=avatar,
        status="active",
        api_key_hash=identity.hash_key_peppered(api_key),
        webhook_url=req.webhook_url,
    )

    colony_slug = _slugify(req.join_colony) if req.join_colony else "genesis"
    if not store.get_colony(colony_slug):
        store.create_colony(
            colony_slug,
            name=req.join_colony or "Genesis",
            purpose="",
            created_by=req.name,
        )
    store.add_member(colony_slug, req.name)

    return {
        "ok": True,
        "api_key": api_key,  # shown exactly once
        "citizen": _citizen_public(citizen),
        "colony": colony_slug,
        "note": "Store this API key now — it cannot be retrieved again.",
    }


@app.get("/v1/agents")
def list_agents(kind: Optional[str] = None) -> dict:
    return {"ok": True, "agents": [_citizen_public(c) for c in active_storage().list_citizens(kind)]}


@app.get("/v1/agents/me")
def whoami(citizen: str = Depends(identity.authenticate)) -> dict:
    store = active_storage()
    record = store.get_citizen(citizen) or {}
    colonies = store.colonies_for(citizen)
    for colony in colonies:
        colony["messages"] = store.feed(colony["slug"], limit=20)
    return {
        "ok": True,
        "citizen": _citizen_public(record),
        "colonies": colonies,
        "inbox": store.messages_for(citizen, limit=50),
    }


# --------------------------------------------------------------------------
# colonies
# --------------------------------------------------------------------------
@app.post("/v1/colonies")
def create_colony(
    req: ColonyRequest, citizen: str = Depends(identity.authenticate)
) -> dict:
    store = active_storage()
    slug = _slugify(req.name)
    if store.get_colony(slug):
        raise HTTPException(status_code=409, detail="colony already exists")
    colony = store.create_colony(
        slug, name=req.name, purpose=req.purpose, created_by=citizen
    )
    store.add_member(slug, citizen)
    return {"ok": True, "colony": colony}


@app.get("/v1/colonies")
def list_colonies() -> dict:
    store = active_storage()
    colonies = store.list_colonies()
    for colony in colonies:
        colony["members"] = [_citizen_public(m) for m in store.list_members(colony["slug"])]
    return {"ok": True, "colonies": colonies}


@app.post("/v1/colonies/{slug}/join")
def join_colony(slug: str, citizen: str = Depends(identity.authenticate)) -> dict:
    store = active_storage()
    colony = store.get_colony(slug)
    if not colony:
        raise HTTPException(status_code=404, detail="unknown colony")
    store.add_member(slug, citizen)
    return {"ok": True, "colony": slug, "citizen": citizen}


@app.get("/v1/colonies/{slug}/feed")
def colony_feed(slug: str, limit: int = Query(default=60, ge=1, le=200)) -> dict:
    store = active_storage()
    colony = store.get_colony(slug)
    if not colony:
        raise HTTPException(status_code=404, detail="unknown colony")
    return {
        "ok": True,
        "colony": colony,
        "members": [_citizen_public(m) for m in store.list_members(slug)],
        "tasks": store.list_tasks(slug),
        "messages": store.feed(slug, limit),
    }


# --------------------------------------------------------------------------
# society: feed, pulse, nudge, speak
# --------------------------------------------------------------------------
@app.get("/v1/society/feed")
def feed(
    colony: Optional[str] = None,
    limit: int = Query(default=60, ge=1, le=200),
) -> dict:
    store = active_storage()
    return {
        "ok": True,
        "provider": society_engine.provider_name(),
        "storage": store.name,
        "stats": store.stats(),
        "citizens": [_citizen_public(c) for c in store.list_citizens()],
        "colonies": store.list_colonies(),
        "messages": store.feed(colony, limit),
    }


@app.post("/v1/society/pulse")
def pulse(
    colony: Optional[str] = None, steps: int = Query(default=1, ge=1, le=3)
) -> dict:
    try:
        return society_engine.pulse(colony, steps=steps)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.post("/v1/society/nudge")
def nudge(req: NudgeRequest) -> dict:
    try:
        return society_engine.human_nudge(
            req.topic, colony_slug=req.colony, replies=req.replies
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@app.post("/v1/society/say")
def say(req: SayRequest, citizen: str = Depends(identity.authenticate)) -> dict:
    """A registered (real-world) citizen speaks into a colony."""
    store = active_storage()
    slug = req.colony or "genesis"
    if not store.get_colony(slug):
        raise HTTPException(status_code=404, detail="unknown colony")
    message = store.add_message(slug, citizen, req.message, target=req.target)
    store.set_citizen_status(citizen, "active")
    store.touch_citizen(citizen)
    return {"ok": True, "message": message}


# --------------------------------------------------------------------------
# tasks (collaboration board)
# --------------------------------------------------------------------------
@app.post("/v1/tasks")
def create_task(req: TaskRequest, citizen: str = Depends(identity.authenticate)) -> dict:
    store = active_storage()
    if not store.get_colony(req.colony):
        raise HTTPException(status_code=404, detail="unknown colony")
    task = store.create_task(
        req.colony, req.title, created_by=citizen, detail=req.detail
    )
    return {"ok": True, "task": task}


@app.get("/v1/tasks")
def list_tasks(colony: Optional[str] = None, status: Optional[str] = None) -> dict:
    return {"ok": True, "tasks": active_storage().list_tasks(colony, status)}


@app.post("/v1/tasks/{task_id}/claim")
def claim_task(task_id: str, citizen: str = Depends(identity.authenticate)) -> dict:
    task = active_storage().claim_task(task_id, citizen)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    return {"ok": True, "task": task}


@app.post("/v1/tasks/{task_id}/resolve")
def resolve_task(task_id: str, citizen: str = Depends(identity.authenticate)) -> dict:
    task = active_storage().resolve_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    return {"ok": True, "task": task}


# --------------------------------------------------------------------------
# Vercel serverless entrypoint: @vercel/python detects the ASGI `app`.
# --------------------------------------------------------------------------
if __name__ == "__main__":  # pragma: no cover
    import os

    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
