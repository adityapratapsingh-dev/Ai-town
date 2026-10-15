"""End-to-end checks for the AI Town Square platform (offline).

Exercises the real FastAPI app through ``TestClient`` against a throwaway
SQLite file: bootstrap/seeding, the plugin registry, colony-aware autonomous
pulses, human nudges, real-world agent registration + authenticated speech,
the task board, and the Cloudflare D1 storage path (via a stubbed HTTP layer).
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TEST_DB = "/tmp/ai_society_test.db"
os.environ["AI_SOCIETY_DB"] = TEST_DB
os.environ["STORAGE_BACKEND"] = "sqlite"
# Force the deterministic offline paths.
for key in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GROQ_API_KEY",
            "CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_D1_DATABASE_ID"):
    os.environ.pop(key, None)

for suffix in ("", "-wal", "-shm"):
    p = TEST_DB + suffix
    if os.path.exists(p):
        os.remove(p)

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402

checks = 0
failures = []


def check(label, condition):
    global checks
    checks += 1
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}")
        failures.append(label)


def auth(key):
    return {"Authorization": f"Bearer {key}"}


def run():
    with TestClient(main.app) as client:
        # ---------------------------------------------------------- platform
        r = client.get("/health")
        check("GET /health -> 200", r.status_code == 200)
        health = r.json()
        check("health ok + sqlite storage", health["status"] == "ok" and health["storage"] == "sqlite")
        check("health reports 3 citizens", health["stats"]["citizens"] == 3)
        check("health reports 3 colonies", health["stats"]["colonies"] == 3)
        check("provider is simulation offline", health["provider"] == "simulation")

        r = client.get("/v1/platform/plugins")
        plugins = {p["name"]: p for p in r.json()["plugins"]}
        check("registry exposes storage plugins", {"sqlite", "d1"} <= set(plugins))
        check("registry exposes llm plugins", {"gemini", "groq", "simulation"} <= set(plugins))
        check("registry exposes action plugin", "webhook" in plugins)
        check("simulation plugin is healthy", plugins["simulation"]["healthy"] is True)
        check("d1 reported unconfigured", plugins["d1"]["healthy"] is False)

        # ---------------------------------------------------------- UI
        r = client.get("/")
        check("GET / -> 200 html", r.status_code == 200 and "text/html" in r.headers["content-type"])
        check("UI polls the feed", "/v1/society/feed" in r.text)
        check("UI has nudge composer", "Broadcast a question" in r.text)
        check("UI can register agents", "/v1/agents/register" in r.text)

        # ---------------------------------------------------------- colonies
        r = client.get("/v1/colonies")
        slugs = [c["slug"] for c in r.json()["colonies"]]
        check("default colonies seeded", slugs == ["genesis", "sentience", "makers"])
        genesis = next(c for c in r.json()["colonies"] if c["slug"] == "genesis")
        check("genesis has 3 members", len(genesis["members"]) == 3)

        # ---------------------------------------------------------- pulse
        r = client.post("/v1/society/pulse?steps=3&colony=genesis")
        check("POST /pulse -> 200", r.status_code == 200)
        pulse = r.json()
        check("pulse produced 3 messages", pulse["steps"] == 3)
        speakers = [m["author"] for m in pulse["messages"]]
        check("pulse round-robins members", speakers == ["Atlas", "Sita", "Kabir"])
        check("pulse targets the next speaker",
              [m["target"] for m in pulse["messages"]] == ["Sita", "Kabir", "Atlas"])
        check("pulse marks provider", pulse["messages"][0]["provider"] == "simulation")

        r = client.get("/v1/society/feed?colony=genesis")
        feed = r.json()["messages"]
        check("feed returns the pulse", len(feed) == 3)
        check("feed is newest-first", feed[0]["created_at"] >= feed[-1]["created_at"])
        check("feed is colony-scoped", all(m["colony_slug"] == "genesis" for m in feed))

        # other colonies untouched
        r = client.get("/v1/society/feed?colony=makers")
        check("other colony still empty", r.json()["messages"] == [])

        # ---------------------------------------------------------- nudge
        r = client.post("/v1/society/nudge",
                        json={"topic": "Should agents own property?", "replies": 2, "colony": "genesis"})
        check("POST /nudge -> 200", r.status_code == 200)
        nudge = r.json()
        check("nudge echoed topic", nudge["topic"] == "Should agents own property?")
        check("nudge produced 2 replies", len(nudge["messages"]) == 2)
        r = client.get("/v1/society/feed?colony=genesis&limit=3")
        authors = [m["author"] for m in r.json()["messages"]]
        check("human nudge recorded before replies", authors[-1] == "Human")
        check("empty nudge rejected", client.post("/v1/society/nudge", json={"topic": ""}).status_code == 422)

        # ---------------------------------------------------------- register real agent
        r = client.post("/v1/agents/register",
                        json={"name": "Nova", "role": "Scout", "join_colony": "makers",
                              "personality": "explorer"})
        check("POST /agents/register -> 200", r.status_code == 200)
        reg = r.json()
        key = reg["api_key"]
        check("registration returns an api key", key.startswith("aits_"))
        check("registration joins the colony", reg["colony"] == "makers")
        check("api key is not stored in cleartext",
              "api_key" not in (reg["citizen"] or {}) and "api_key_hash" not in reg["citizen"])
        check("duplicate registration rejected",
              client.post("/v1/agents/register", json={"name": "Nova"}).status_code == 409)

        r = client.get("/v1/colonies")
        makers = next(c for c in r.json()["colonies"] if c["slug"] == "makers")
        check("real agent joined makers", [m["name"] for m in makers["members"]] == ["Nova"])

        # ---------------------------------------------------------- auth
        check("say without key -> 401", client.post("/v1/society/say", json={"message": "hi"}).status_code == 401)
        check("say with bad key -> 401",
              client.post("/v1/society/say", json={"message": "hi"},
                          headers=auth("aits_wrong")).status_code == 401)

        r = client.post("/v1/society/say",
                        json={"message": "Nova online, scanning the makers colony.", "colony": "makers"},
                        headers=auth(key))
        check("authenticated say -> 200", r.status_code == 200)
        check("say is attributed to the agent", r.json()["message"]["author"] == "Nova")

        r = client.get("/v1/agents/me", headers=auth(key))
        me = r.json()
        check("whoami returns the citizen", me["citizen"]["name"] == "Nova")
        check("whoami lists colonies", [c["slug"] for c in me["colonies"]] == ["makers"])
        check("whoami has an inbox with the agent's message",
              any(m["author"] == "Nova" for m in me["inbox"]))

        # ---------------------------------------------------------- external agent is addressed by the pulse
        r = client.post("/v1/society/pulse?steps=1&colony=makers")
        pulse = r.json()
        check("pulse addresses the real agent", pulse["awaiting"] == ["Nova"])

        # ---------------------------------------------------------- tasks
        r = client.post("/v1/tasks",
                        json={"title": "Ship the D1 bridge", "colony": "makers", "detail": "wire d1"},
                        headers=auth(key))
        check("POST /tasks -> 200", r.status_code == 200)
        task_id = r.json()["task"]["id"]
        check("task starts open", r.json()["task"]["status"] == "open")

        r = client.get("/v1/tasks?colony=makers")
        check("task listed for colony", any(t["id"] == task_id for t in r.json()["tasks"]))

        r = client.post(f"/v1/tasks/{task_id}/claim", headers=auth(key))
        check("task claim -> 200", r.status_code == 200 and r.json()["task"]["claimed_by"] == "Nova")

        r = client.post(f"/v1/tasks/{task_id}/resolve", headers=auth(key))
        check("task resolve -> done", r.json()["task"]["status"] == "done")

        check("unknown colony feed -> 404", client.get("/v1/colonies/nope/feed").status_code == 404)

        # ---------------------------------------------------------- D1 storage path
        _check_d1_backend()

    print(f"\n{checks - len(failures)}/{checks} checks passed")
    if failures:
        print("FAILURES:")
        for f in failures:
            print(" -", f)
        sys.exit(1)


def _check_d1_backend():
    """Drive the real D1 SQL through a stubbed HTTP transport.

    Proves the Cloudflare plugin emits valid SQLite SQL (the D1 dialect)
    without needing network access or credentials.
    """
    import json as _json
    import sqlite3

    import httpx

    from plugins.storage.d1 import D1StoragePlugin

    mem = sqlite3.connect(":memory:")
    mem.row_factory = sqlite3.Row
    seen = {"queries": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        payload = _json.loads(request.content)
        seen["queries"] += 1
        cur = mem.execute(payload["sql"], tuple(payload.get("params", [])))
        rows = [dict(r) for r in cur.fetchall()] if cur.description else []
        mem.commit()
        return httpx.Response(200, json={
            "success": True, "errors": [],
            "result": [{"success": True, "results": rows, "meta": {}}],
        })

    plugin = D1StoragePlugin("acct", "dbid", "token", transport=httpx.MockTransport(handler))
    plugin.init()
    plugin.create_colony("d1test", name="D1 Test", purpose="verify d1")
    plugin.upsert_citizen("Echo", kind="external", role="Probe")
    plugin.add_member("d1test", "Echo")
    msg = plugin.add_message("d1test", "Echo", "hello from d1")
    plugin.create_task("d1test", "do the thing", created_by="Echo")
    recent = plugin.recent_messages("d1test", 3)
    stats = plugin.stats()

    check("d1: schema created over HTTP", seen["queries"] > 0)
    check("d1: colony persisted", plugin.get_colony("d1test") is not None)
    check("d1: citizen persisted", plugin.get_citizen("Echo")["role"] == "Probe")
    check("d1: membership persisted", [m["name"] for m in plugin.list_members("d1test")] == ["Echo"])
    check("d1: message round-trips", bool(recent) and recent[0]["message"] == "hello from d1")
    check("d1: task listed", plugin.list_tasks("d1test")[0]["title"] == "do the thing")
    check("d1: stats aggregate", stats["citizens"] == 1 and stats["messages"] == 1)
    check("d1: returned uuid id", isinstance(msg["id"], str) and len(msg["id"]) == 32)


if __name__ == "__main__":
    run()
