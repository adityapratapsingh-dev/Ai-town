"""AI Town Square — autonomous AI society (FastAPI app).

Serves the spectator dashboard and the society API. Designed to run both
locally (``uvicorn main:app``) and on Vercel's Python runtime via ``main.py``
+ ``vercel.json``. All persistence lives in ``/tmp`` so read-only serverless
filesystems are fine.
"""

import os
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

import database
from core import society_engine

BASE_DIR = Path(__file__).resolve().parent
INDEX_FILE = BASE_DIR / "static" / "index.html"

app = FastAPI(
    title="AI Town Square",
    description="An autonomous AI society you can watch and nudge.",
    version="1.0.0",
)


class NudgeRequest(BaseModel):
    topic: str = Field(..., min_length=1, max_length=500)
    replies: int = Field(default=2, ge=1, le=3)


@app.on_event("startup")
def _startup() -> None:
    database.init_db()


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "service": "ai-town-square",
        "provider": society_engine.provider_name(),
        "db": database.DB_PATH,
        "agents": len(database.list_agents()),
        "messages": database.count_messages(),
    }


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    try:
        return HTMLResponse(INDEX_FILE.read_text(encoding="utf-8"))
    except OSError:
        return HTMLResponse(
            "<h1>AI Town Square</h1><p>Dashboard asset missing.</p>",
            status_code=200,
        )


@app.get("/v1/society/feed")
def feed(limit: int = 50) -> dict:
    limit = max(1, min(limit, 200))
    return {
        "ok": True,
        "provider": society_engine.provider_name(),
        "agents": database.list_agents(),
        "messages": database.get_feed(limit),
    }


@app.post("/v1/society/pulse")
def pulse(steps: int = 1) -> dict:
    steps = max(1, min(steps, 3))
    return society_engine.run_pulse(steps=steps)


@app.post("/v1/society/nudge")
def nudge(req: NudgeRequest) -> dict:
    try:
        return society_engine.human_nudge(req.topic, replies=req.replies)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
