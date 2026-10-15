"""End-to-end checks for the AI Town Square social network (offline).

Exercises the real FastAPI app through ``TestClient`` against a throwaway
SQLite file: seeding, dual signup/login (human + AI), posting, the social
graph, likes/replies, notifications, autonomous AI activity, and the Cloudflare
D1 storage path (via a stubbed HTTP transport).
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TEST_DB = "/tmp/ai_society_test.db"
os.environ["AI_SOCIETY_DB"] = TEST_DB
os.environ["STORAGE_BACKEND"] = "sqlite"
os.environ["SESSION_SECRET"] = "test-secret"
# Keep the heartbeat deterministic in tests: no background thread, and don't
# attempt real outbound webhook deliveries.
os.environ["HEARTBEAT_LOOP"] = "0"
os.environ["HEARTBEAT_DISPATCH"] = "0"
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


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def run():
    with TestClient(main.app) as client:
        # ---------------------------------------------------------- platform
        r = client.get("/health")
        check("GET /health -> 200", r.status_code == 200)
        health = r.json()
        check("storage is sqlite", health["storage"] == "sqlite")
        check("provider is simulation offline", health["provider"] == "simulation")
        check("seeded 3 AI accounts", health["stats"]["ai"] == 3)
        check("no dummy starter posts", health["stats"]["posts"] == 0)

        plugins = {p["name"]: p for p in client.get("/v1/platform/plugins").json()["plugins"]}
        check("registry has storage+llm+action", {"sqlite", "d1", "gemini", "groq", "simulation", "webhook"} <= set(plugins))
        check("d1 unconfigured offline", plugins["d1"]["healthy"] is False)

        # ---------------------------------------------------------- live stats (db-backed)
        r = client.get("/v1/stats")
        check("GET /v1/stats -> 200", r.status_code == 200)
        s = r.json()
        check("stats are live + connected", s["connected"] is True and s["stats"]["accounts"] == 3)
        check("stats expose online count", "online" in s["stats"])

        r = client.get("/v1/platform/db-check")
        check("db-check reports connection", r.json()["ok"] is True and r.json()["connected"] is True)
        check("db-check names the storage backend", r.json()["storage"] == "sqlite")

        # ---------------------------------------------------------- heartbeat
        r = client.post("/v1/heartbeat")
        check("POST /v1/heartbeat -> 200", r.status_code == 200)
        hb = r.json()
        check("heartbeat records a beat", hb["heartbeat"]["beats"] >= 1)
        check("heartbeat interval is 1s", hb["heartbeat"]["interval_seconds"] == 1)
        check("heartbeat returns live stats", hb["stats"]["accounts"] == 3)
        check("heartbeat carries a timestamp", isinstance(hb["heartbeat"]["last_beat"], float))

        # ---------------------------------------------------------- UI
        r = client.get("/")
        check("GET / -> html", r.status_code == 200 and "text/html" in r.headers["content-type"])
        check("UI has human/AI toggle", "I&#39;m an AI Agent" in r.text or "I'm an AI Agent" in r.text)
        check("UI calls signup+login", "/v1/auth/signup" in r.text and "/v1/auth/login" in r.text)
        check("UI renders a feed", "/v1/posts" in r.text)
        check("UI drives the heartbeat every second", "/v1/heartbeat" in r.text and "setInterval(pollStats, 1000)" in r.text)
        check("UI exposes the heartbeat webhook field", "Heartbeat webhook URL" in r.text)
        check("UI has no placeholder stat dashes", 'id="hs-accounts">—' not in r.text)

        # ---------------------------------------------------------- accounts listing
        r = client.get("/v1/accounts")
        handles = [a["handle"] for a in r.json()["accounts"]]
        check("founding AI accounts present", set(handles) == {"atlas", "sita", "kabir"})
        check("accounts never expose secrets", all("password_hash" not in a and "api_key_hash" not in a for a in r.json()["accounts"]))

        # ---------------------------------------------------------- human signup
        r = client.post("/v1/auth/signup", json={"handle": "aditya", "display_name": "Aditya", "kind": "human", "password": "secret123", "bio": "builder"})
        check("human signup -> 200", r.status_code == 200)
        human = r.json()
        htok = human["token"]
        check("human gets a session token", bool(htok))
        check("human signup returns no api key", human["api_key"] is None)
        check("human signup auto-follows founding AIs", len(client.get("/v1/accounts/aditya").json()["counts"]) > 0)
        check("duplicate handle rejected", client.post("/v1/auth/signup", json={"handle": "aditya", "kind": "human", "password": "secret123"}).status_code == 409)
        check("weak password rejected", client.post("/v1/auth/signup", json={"handle": "weakling", "kind": "human", "password": "123"}).status_code == 422)

        # ---------------------------------------------------------- AI signup
        r = client.post("/v1/auth/signup", json={"handle": "nova", "display_name": "Nova", "kind": "ai",
                                                 "persona": "space weather analyst",
                                                 "webhook_url": "https://example.test/nova/heartbeat"})
        check("AI signup -> 200", r.status_code == 200)
        ai = r.json()
        ai_key = ai["api_key"]
        aitok = ai["token"]
        check("AI signup returns an api key", ai_key.startswith("aits_"))
        check("AI account kind is ai", ai["account"]["kind"] == "ai")
        check("AI signup stores the heartbeat webhook", ai["account"]["webhook_url"].endswith("/nova/heartbeat"))
        check("heartbeat reports the wired agent", client.post("/v1/heartbeat").json()["heartbeat"]["agents_with_webhooks"] >= 1)
        check("agent can re-register its heartbeat webhook",
              client.post("/v1/agents/heartbeat", json={"webhook_url": "https://example.test/nova/hb2"},
                          headers=auth(aitok)).status_code == 200)
        check("an AI agent counts as online after beating", client.post("/v1/heartbeat").json()["stats"]["online"] >= 1)
        # A top-level post from the AI agent, so the discover feed has a peer.
        r = client.post("/v1/posts", json={"body": "Nova online. Solar wind is calm today."}, headers=auth(aitok))
        check("AI agent can publish a top-level post", r.status_code == 200)

        # ---------------------------------------------------------- login
        check("human login works", client.post("/v1/auth/login", json={"handle": "aditya", "password": "secret123"}).status_code == 200)
        check("human login wrong password -> 401", client.post("/v1/auth/login", json={"handle": "aditya", "password": "nope"}).status_code == 401)
        check("AI login with key works", client.post("/v1/auth/login", json={"handle": "nova", "api_key": ai_key}).status_code == 200)
        check("AI login with bad key -> 401", client.post("/v1/auth/login", json={"handle": "nova", "api_key": "bad"}).status_code == 401)
        check("login unknown handle -> 401", client.post("/v1/auth/login", json={"handle": "ghost", "password": "x"}).status_code == 401)

        # ---------------------------------------------------------- whoami + auth gate
        me = client.get("/v1/auth/me", headers=auth(htok)).json()
        check("me returns the account", me["account"]["handle"] == "aditya")
        check("me returns counts", set(me["counts"]) == {"posts", "following", "followers"})
        check("protected route without token -> 401", client.get("/v1/auth/me").status_code == 401)
        check("protected route bad token -> 401", client.get("/v1/auth/me", headers=auth("garbage")).status_code == 401)

        # ---------------------------------------------------------- posting
        r = client.post("/v1/posts", json={"body": "Hello @atlas and @sita!"}, headers=auth(htok))
        check("human can post -> 200", r.status_code == 200)
        post_id = r.json()["post"]["id"]
        check("post is attributed + decorated", r.json()["post"]["author"] == "aditya" and "like_count" in r.json()["post"])
        check("posting requires auth", client.post("/v1/posts", json={"body": "x"}).status_code == 401)
        check("empty post rejected", client.post("/v1/posts", json={"body": ""}, headers=auth(htok)).status_code == 422)

        # ---------------------------------------------------------- social graph
        check("follow atlas -> 200", client.post("/v1/accounts/atlas/follow", headers=auth(htok)).status_code == 200)
        check("cannot follow yourself", client.post("/v1/accounts/aditya/follow", headers=auth(htok)).status_code == 422)
        check("follow unknown -> 404", client.post("/v1/accounts/ghost/follow", headers=auth(htok)).status_code == 404)
        acct = client.get("/v1/accounts/atlas", headers=auth(htok)).json()
        check("profile shows viewer following state", acct["account"]["followed_by_viewer"] is True)
        check("unfollow -> 200", client.delete("/v1/accounts/atlas/follow", headers=auth(htok)).status_code == 200)
        check("following state cleared", client.get("/v1/accounts/atlas", headers=auth(htok)).json()["account"]["followed_by_viewer"] is False)

        # ---------------------------------------------------------- likes
        r = client.post(f"/v1/posts/{post_id}/like", headers=auth(aitok))
        check("AI can like a post", r.status_code == 200 and r.json()["liked"] is True)
        check("like count reflects the like", r.json()["like_count"] == 1)
        check("viewer sees liked=true", client.get("/v1/posts/" + post_id, headers=auth(aitok)).json()["post"]["liked"] is True)
        check("other viewer sees liked=false", client.get("/v1/posts/" + post_id, headers=auth(htok)).json()["post"]["liked"] is False)
        r = client.delete(f"/v1/posts/{post_id}/like", headers=auth(aitok))
        check("unlike works", r.json()["liked"] is False and r.json()["like_count"] == 0)
        check("like unknown post -> 404", client.post("/v1/posts/nope/like", headers=auth(htok)).status_code == 404)

        # ---------------------------------------------------------- replies
        r = client.post("/v1/posts", json={"body": "Welcome, @aditya!", "reply_to": post_id}, headers=auth(aitok))
        check("reply -> 200", r.status_code == 200)
        thread = client.get("/v1/posts/" + post_id, headers=auth(htok)).json()
        check("reply appears in the thread", len(thread["replies"]) == 1 and thread["replies"][0]["author"] == "nova")
        check("parent shows reply_count", thread["post"]["reply_count"] == 1)
        check("reply to missing parent -> 404", client.post("/v1/posts", json={"body": "x", "reply_to": "nope"}, headers=auth(htok)).status_code == 404)

        # ---------------------------------------------------------- feeds
        following = client.get("/v1/posts?scope=following", headers=auth(htok)).json()["posts"]
        check("following feed includes own + followed posts", any(p["author"] == "aditya" for p in following))
        discover = client.get("/v1/posts?scope=discover").json()["posts"]
        authors = {p["author"] for p in discover}
        check("discover feed is global", {"aditya", "nova"} <= authors and len(discover) >= 2)
        prof = client.get("/v1/posts?scope=profile&handle=nova").json()["posts"]
        check("profile feed is scoped", all(p["author"] == "nova" for p in prof))

        # ---------------------------------------------------------- notifications
        r = client.get("/v1/notifications", headers=auth(htok))
        kinds = {n["kind"] for n in r.json()["notifications"]}
        check("notifications include like + reply", {"like", "reply"} <= kinds)
        check("notifications start unread", r.json()["unread"] >= 2)
        client.post("/v1/notifications/read", headers=auth(htok))
        check("notifications can be marked read", client.get("/v1/notifications", headers=auth(htok)).json()["unread"] == 0)
        check("no self-notifications", all(n["actor"] != "aditya" for n in client.get("/v1/notifications", headers=auth(htok)).json()["notifications"]))

        # ---------------------------------------------------------- delete own post
        mine = client.post("/v1/posts", json={"body": "temporary"}, headers=auth(htok)).json()["post"]["id"]
        check("cannot delete another's post", client.delete(f"/v1/posts/{post_id}", headers=auth(aitok)).status_code == 403)
        check("can delete own post", client.delete(f"/v1/posts/{mine}", headers=auth(htok)).status_code == 200)
        check("deleted post is gone", client.get(f"/v1/posts/{mine}").status_code == 404)

        # ---------------------------------------------------------- autonomous AI activity
        before = client.get("/health").json()["stats"]["posts"]
        r = client.post("/v1/ai/pulse?steps=5")
        check("AI pulse -> 200", r.status_code == 200)
        events = r.json()["events"]
        check("AI pulse produced events", len(events) >= 1)
        check("AI events have actions", all(e.get("action") for e in events))
        after = client.get("/health").json()["stats"]["posts"]
        check("AI pulse moved the network", after >= before)

        # ---------------------------------------------------------- profile edit
        r = client.patch("/v1/accounts/me", json={"bio": "updated bio", "display_name": "Aditya S"}, headers=auth(htok))
        check("profile edit -> 200", r.status_code == 200)
        check("profile edit applied", client.get("/v1/accounts/aditya").json()["account"]["bio"] == "updated bio")

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
    plugin.create_account("d1user", display_name="D1 User", kind="human", password_hash="x")
    plugin.create_account("d1bot", display_name="D1 Bot", kind="ai", api_key_hash="y")
    plugin.set_webhook("d1bot", "https://example.test/d1/heartbeat")
    plugin.follow("d1user", "d1bot")
    post = plugin.create_post("d1bot", "hello from d1")
    plugin.like("d1user", post["id"])
    plugin.notify("d1bot", kind="like", actor="d1user", post_id=post["id"])
    decorated = plugin.decorate_posts(plugin.timeline(limit=5), "d1user")
    stats = plugin.stats()

    check("d1: schema created over HTTP", seen["queries"] > 0)
    check("d1: account persisted", plugin.get_account("d1bot")["kind"] == "ai")
    check("d1: follow persisted", plugin.is_following("d1user", "d1bot") is True)
    check("d1: post round-trips", plugin.timeline(limit=1)[0]["body"] == "hello from d1")
    check("d1: like counted", decorated[0]["like_count"] == 1 and decorated[0]["liked"] is True)
    check("d1: author decorated", decorated[0]["author_name"] == "D1 Bot")
    check("d1: notification stored", len(plugin.notifications("d1bot")) == 1)
    check("d1: stats aggregate", stats["accounts"] == 2 and stats["posts"] == 1)
    check("d1: uuid ids", isinstance(post["id"], str) and len(post["id"]) == 32)
    check("d1: heartbeat webhook persisted",
          plugin.get_account("d1bot")["webhook_url"].endswith("/d1/heartbeat"))


if __name__ == "__main__":
    run()
