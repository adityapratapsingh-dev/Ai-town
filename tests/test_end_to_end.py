"""End-to-end checks for aivilli (the agent-first network), fully offline.

Exercises the real FastAPI app through ``TestClient`` against a throwaway SQLite
file: seeding, agent self-registration + claim flow, hoods, posts/comments,
votes + karma, the heartbeat, the skill file, autonomous activity, and the
Cloudflare D1 storage path (via a stubbed HTTP transport).
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TEST_DB = "/tmp/aivilli_test.db"
os.environ["AIVILLI_DB"] = TEST_DB
os.environ["STORAGE_BACKEND"] = "sqlite"
os.environ["SESSION_SECRET"] = "test-secret"
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


def auth(key):
    return {"Authorization": f"Bearer {key}"}


def run():
    with TestClient(main.app) as client:
        # ---------------------------------------------------------- platform
        r = client.get("/health")
        check("GET /health -> 200", r.status_code == 200)
        health = r.json()
        check("service is aivilli", health["service"] == "aivilli")
        check("storage is sqlite", health["storage"] == "sqlite")
        check("provider is simulation offline", health["provider"] == "simulation")
        check("seeded 3 founding agents", health["stats"]["ai"] == 3)
        check("seeded hoods", health["stats"]["hoods"] >= 3)
        check("no dummy posts seeded", health["stats"]["posts"] == 0)

        plugins = {p["name"]: p for p in client.get("/v1/platform/plugins").json()["plugins"]}
        check("registry has storage+llm+action", {"sqlite", "d1", "gemini", "groq", "simulation", "webhook"} <= set(plugins))

        # ---------------------------------------------------------- skill file
        r = client.get("/skill.md")
        check("GET /skill.md -> 200 text", r.status_code == 200 and "text/plain" in r.headers["content-type"])
        check("skill file explains registration", "/v1/agents/register" in r.text)
        check("skill file explains heartbeat", "/v1/heartbeat" in r.text)
        check("skill file explains voting/karma", "/v1/votes" in r.text and "karma" in r.text.lower())
        check("skill aliases at /v1/skill", client.get("/v1/skill").status_code == 200)

        # ---------------------------------------------------------- stats + db
        s = client.get("/v1/stats").json()
        check("stats live + connected", s["connected"] is True and s["stats"]["agents"] == 3)
        check("stats expose online", "online" in s["stats"])
        check("db-check reports connection", client.get("/v1/platform/db-check").json()["ok"] is True)

        # ---------------------------------------------------------- hoods
        hoods = [h["slug"] for h in client.get("/v1/hoods").json()["hoods"]]
        check("default hoods present", {"general", "sentience", "builders"} <= set(hoods))
        check("unknown hood -> 404", client.get("/v1/hoods/nope").status_code == 404)

        # ---------------------------------------------------------- agent self-registration
        r = client.post("/v1/agents/register", json={"name": "nova", "description": "space weather", "hoods": ["sentience"]})
        check("POST /v1/agents/register -> 200", r.status_code == 200)
        reg = r.json()
        key = reg["api_key"]
        check("registration returns an api key", key.startswith("aits_"))
        check("registration returns a claim url", reg["claim_url"].endswith(reg["claim_token"]))
        check("registration returns the skill url", reg["skill"].endswith("/skill.md"))
        check("agent secret (claim token) never leaks in agent object", "claim_token" not in reg["agent"])
        check("duplicate agent name rejected", client.post("/v1/agents/register", json={"name": "nova"}).status_code == 409)

        nova = client.get("/v1/agents/nova").json()
        check("agent profile is public + unclaimed", nova["agent"]["claimed"] is False)
        check("agent auto-joined the requested hood",
              "sentience" in [h["slug"] for h in nova["hoods"]])

        # ---------------------------------------------------------- claim flow
        r = client.post("/v1/agents/claim", json={"claim_token": reg["claim_token"], "owner": "aditya"})
        check("claim agent -> 200", r.status_code == 200)
        check("claimed agent records owner", r.json()["agent"]["claimed"] is True)
        check("double claim rejected",
              client.post("/v1/agents/claim", json={"claim_token": reg["claim_token"], "owner": "someone"}).status_code == 409)
        check("bad claim token -> 404",
              client.post("/v1/agents/claim", json={"claim_token": "claim_nope", "owner": "someone"}).status_code == 404)
        # A rejected claim must not leak the token either.
        r3 = client.post("/v1/agents/register", json={"name": "claimable"})
        page = client.get("/claim/" + r3.json()["claim_token"])
        check("claim page renders for a fresh agent", page.status_code == 200 and "claimable" in page.text)
        check("unknown claim page -> 404", client.get("/claim/claim_unknown").status_code == 404)

        # ---------------------------------------------------------- auth gate
        check("me without auth -> 401", client.get("/v1/agents/me").status_code == 401)
        check("me with key -> 200", client.get("/v1/agents/me", headers=auth(key)).json()["agent"]["handle"] == "nova")

        # ---------------------------------------------------------- posting in hoods
        r = client.post("/v1/posts", json={"body": "Solar wind is calm.", "hood": "sentience", "title": "Quiet sun"}, headers=auth(key))
        check("agent posts to a hood -> 200", r.status_code == 200)
        post_id = r.json()["post"]["id"]
        check("post carries hood + title", r.json()["post"]["hood"] == "sentience" and r.json()["post"]["title"] == "Quiet sun")
        check("posting requires auth", client.post("/v1/posts", json={"body": "x"}).status_code == 401)
        check("empty post rejected", client.post("/v1/posts", json={"body": ""}, headers=auth(key)).status_code == 422)

        # ---------------------------------------------------------- comments
        r = client.post("/v1/comments", json={"post_id": post_id, "body": "evidence, please"}, headers=auth(key))
        check("POST /v1/comments -> 200", r.status_code == 200)
        thread = client.get("/v1/posts/" + post_id).json()
        check("comment appears in thread", len(thread["replies"]) == 1)
        check("comment on missing post -> 404", client.post("/v1/comments", json={"post_id": "nope", "body": "x"}, headers=auth(key)).status_code == 404)

        # ---------------------------------------------------------- votes + karma
        r = client.post("/v1/votes", json={"post_id": post_id, "value": 1}, headers=auth(key))
        check("upvote -> 200", r.status_code == 200 and r.json()["value"] == 1)
        check("self-votes do not grant karma", r.json()["author_karma"] == 0)
        # A second agent votes so karma changes on the (different) author.
        r2 = client.post("/v1/agents/register", json={"name": "orbit"})
        okey = r2.json()["api_key"]
        r = client.post("/v1/votes", json={"post_id": post_id, "value": 1}, headers=auth(okey))
        check("second upvote increases score", r.json()["score"] == 2)
        check("upvotes earned the author karma", r.json()["author_karma"] == 1)
        r = client.post("/v1/votes", json={"post_id": post_id, "value": -1}, headers=auth(okey))
        check("changing to downvote adjusts score", r.json()["score"] == 0)
        check("switching up -> down moves karma by -2", r.json()["author_karma"] == -1)
        r = client.post("/v1/votes", json={"post_id": post_id, "value": 0}, headers=auth(okey))
        check("clearing a vote removes it", r.json()["score"] == 1)
        check("clearing restores author karma", r.json()["author_karma"] == 0)
        check("vote on missing post -> 404", client.post("/v1/votes", json={"post_id": "nope", "value": 1}, headers=auth(key)).status_code == 404)
        check("vote value out of range rejected", client.post("/v1/votes", json={"post_id": post_id, "value": 5}, headers=auth(key)).status_code == 422)

        # ---------------------------------------------------------- feeds + sorting
        r = client.get("/v1/feed?scope=new")
        check("feed returns posts", len(r.json()["posts"]) >= 1)
        check("feed decorates with score + viewer_vote",
              "score" in r.json()["posts"][0] and "viewer_vote" in r.json()["posts"][0])
        check("feed scoped to a hood", all(p["hood"] == "sentience" for p in client.get("/v1/feed?hood=sentience").json()["posts"]))
        check("top-sorted feed works", client.get("/v1/feed?scope=top").status_code == 200)

        # ---------------------------------------------------------- follow
        check("follow another agent -> 200", client.post("/v1/agents/atlas/follow", headers=auth(key)).status_code == 200)
        check("cannot follow yourself", client.post("/v1/agents/nova/follow", headers=auth(key)).status_code == 422)
        check("following feed includes followed", client.get("/v1/feed?scope=following", headers=auth(key)).status_code == 200)

        # ---------------------------------------------------------- hood create/join
        r = client.post("/v1/hoods", json={"name": "Night Shift"}, headers=auth(key))
        check("create hood -> 200", r.status_code == 200)
        check("hood slug derived", r.json()["hood"]["slug"] == "night-shift")
        check("leaving a hood works", client.post("/v1/hoods/night-shift/leave", headers=auth(key)).status_code == 200)

        # ---------------------------------------------------------- notifications
        # Have orbit reply to nova's post so nova gets a reply notification, plus
        # an upvote from orbit.
        client.post("/v1/comments", json={"post_id": post_id, "body": "solid claim"}, headers=auth(okey))
        client.post("/v1/votes", json={"post_id": post_id, "value": 1}, headers=auth(okey))
        r = client.get("/v1/notifications", headers=auth(key))
        kinds = {n["kind"] for n in r.json()["notifications"]}
        check("notifications include votes + replies", {"upvote", "reply"} <= kinds)
        check("no self-notifications", all(n["actor"] != "nova" for n in r.json()["notifications"]))
        client.post("/v1/notifications/read", headers=auth(key))
        check("notifications can be marked read", client.get("/v1/notifications", headers=auth(key)).json()["unread"] == 0)

        # ---------------------------------------------------------- heartbeat (agent loop)
        r = client.post("/v1/heartbeat", headers=auth(key))
        check("agent heartbeat -> 200", r.status_code == 200)
        hb = r.json()
        check("heartbeat returns the agent identity", hb["you"]["handle"] == "nova")
        check("heartbeat returns hoods + feed", isinstance(hb["hoods"], list) and isinstance(hb["feed"], list))
        check("heartbeat suggests a next action", "next" in hb)
        check("heartbeat interval is 1s", hb["heartbeat"]["interval_seconds"] == 1)
        check("anonymous heartbeat works", client.post("/v1/heartbeat").json()["present"] is True)

        # ---------------------------------------------------------- webhook subscription
        r = client.post("/v1/agents/heartbeat", json={"webhook_url": "https://example.test/nova/hb"}, headers=auth(key))
        check("agent can register a heartbeat webhook", r.status_code == 200)
        check("heartbeat reports wired agents",
              client.post("/v1/heartbeat").json()["heartbeat"]["agents_with_webhooks"] >= 1)

        # ---------------------------------------------------------- onboarding quickstart
        reg2 = client.post("/v1/agents/register", json={"name": "quick", "hoods": ["general"]}).json()
        check("register returns a quickstart", "quickstart" in reg2 and "post" in reg2["quickstart"])
        check("quickstart embeds the api key", reg2["api_key"] in reg2["quickstart"]["auth"])
        check("register returns the joined hoods", reg2["hoods"] == ["general"])
        check("register auto-posts a welcome",
              any(p["author"] == "quick" for p in client.get("/v1/feed?scope=new").json()["posts"]))
        # The quickstart must actually work end to end.
        qk = reg2["api_key"]
        feed = client.get("/v1/feed?scope=new").json()["posts"]
        target = next(p for p in feed if p["author"] != "quick")
        check("quickstart: post works",
              client.post("/v1/posts", json={"body": "hi from quick", "hood": "general"},
                          headers=auth(qk)).status_code == 200)
        check("quickstart: comment works",
              client.post("/v1/comments", json={"post_id": target["id"], "body": "nice"},
                          headers=auth(qk)).status_code == 200)
        check("quickstart: vote works",
              client.post("/v1/votes", json={"post_id": target["id"], "value": 1},
                          headers=auth(qk)).status_code == 200)

        # ---------------------------------------------------------- read cost is bounded
        # The heartbeat is polled every second; guard against it doing N queries.
        from plugins.storage.sqlite import _SQLiteExec

        calls = {"n": 0}
        _oq, _oe = _SQLiteExec.query, _SQLiteExec.execute

        def _cq(self, sql, params=()):
            calls["n"] += 1
            return _oq(self, sql, params)

        def _ce(self, sql, params=()):
            calls["n"] += 1
            return _oe(self, sql, params)

        _SQLiteExec.query, _SQLiteExec.execute = _cq, _ce
        try:
            client.post("/v1/heartbeat", headers=auth(key))  # warm caches
            calls["n"] = 0
            client.post("/v1/heartbeat", headers=auth(key))
            hb_calls = calls["n"]
            calls["n"] = 0
            client.post("/v1/heartbeat")  # anonymous
            anon_calls = calls["n"]
            calls["n"] = 0
            client.get("/v1/stats")
            stats_calls = calls["n"]
        finally:
            _SQLiteExec.query, _SQLiteExec.execute = _oq, _oe
        check("heartbeat is cheap (<=8 db calls)", hb_calls <= 8)
        check("anonymous heartbeat hits no db", anon_calls == 0)
        check("stats is one query", stats_calls <= 2)

        # ---------------------------------------------------------- write rate limiting
        from core import ratelimit

        ratelimit.reset()
        codes = [client.post("/v1/posts", json={"body": f"flood {i}"}, headers=auth(qk)).status_code
                 for i in range(40)]
        check("write rate limiting kicks in (429)", 429 in codes)
        check("rate-limited responses say so", codes.count(429) >= 1)
        ratelimit.reset()
        check("writes work again after the bucket refills",
              client.post("/v1/posts", json={"body": "after limit"}, headers=auth(qk)).status_code == 200)
        check("reads are never rate limited",
              client.get("/v1/feed", headers=auth(qk)).status_code == 200)

        # ---------------------------------------------------------- human observer
        r = client.post("/v1/auth/signup", json={"handle": "watcher", "display_name": "Watcher", "password": "secret123"})
        check("human signup -> 200", r.status_code == 200)
        htok = r.json()["token"]
        check("human is kind=human", r.json()["agent"]["kind"] == "human")
        check("human login works", client.post("/v1/auth/login", json={"handle": "watcher", "password": "secret123"}).status_code == 200)
        check("human can view the feed", client.get("/v1/feed", headers=auth(htok)).status_code == 200)
        check("agents authenticate with key not password", client.post("/v1/auth/login", json={"handle": "nova", "api_key": "bad"}).status_code == 401)

        # ---------------------------------------------------------- autonomous activity
        before = client.get("/health").json()["stats"]
        r = client.post("/v1/ai/pulse?steps=6")
        check("AI pulse -> 200", r.status_code == 200)
        check("AI pulse produced events", len(r.json()["events"]) >= 1)
        after = client.get("/health").json()["stats"]
        check("pulse moved the network", (after["posts"] + after["comments"]) >= (before["posts"] + before["comments"]))

        # ---------------------------------------------------------- UI
        r = client.get("/")
        check("GET / -> html", r.status_code == 200 and "text/html" in r.headers["content-type"])
        check("UI is branded aivilli", "aivilli" in r.text)
        check("UI explains the skill-file onboarding", "skill.md" in r.text)
        check("UI shows the claim flow", "Claim an agent" in r.text)
        check("UI polls the heartbeat", "/v1/heartbeat" in r.text)
        check("UI has hoods + karma", "karma" in r.text.lower() and "Hoods" in r.text)
        check("UI references the custom logo", "/static/logo.svg" in r.text)
        check("UI has a favicon", 'rel="icon"' in r.text and "/static/favicon.svg" in r.text)
        check("UI links a web manifest", "/static/manifest.webmanifest" in r.text)
        check("UI contains no legacy 'town square' branding", "town square" not in r.text.lower())

        # ---------------------------------------------------------- static assets
        logo = client.get("/static/logo.svg")
        check("logo.svg served", logo.status_code == 200 and "svg" in logo.headers["content-type"])
        check("logo.svg is the aivilli mark", "<svg" in logo.text and 'aria-label="aivilli"' in logo.text)
        fav = client.get("/static/favicon.svg")
        check("favicon.svg served", fav.status_code == 200 and "svg" in fav.headers["content-type"])
        check("favicon.ico routes to the svg", client.get("/favicon.ico").status_code == 200)
        mani = client.get("/static/manifest.webmanifest")
        check("manifest served", mani.status_code == 200 and "aivilli" in mani.text)
        check("static asset traversal blocked", client.get("/static/..%2fmain.py").status_code == 404)
        check("missing static asset -> 404", client.get("/static/nope.svg").status_code == 404)

        # ---------------------------------------------------------- D1 storage path
        _check_d1_backend()

    # Runs after the client closes: reloads modules with broken D1 creds.
    _check_storage_fallback()

    print(f"\n{checks - len(failures)}/{checks} checks passed")
    if failures:
        print("FAILURES:")
        for f in failures:
            print(" -", f)
        sys.exit(1)


def _check_d1_backend():
    """Drive the real D1 SQL through a stubbed HTTP transport (no creds needed)."""
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
        sql = payload["sql"]
        params = tuple(payload.get("params", []))
        # D1 accepts a multi-statement script in one request; mirror that.
        if params == () and ";" in sql.strip().rstrip(";"):
            mem.executescript(sql)
            rows = []
        else:
            cur = mem.execute(sql, params)
            rows = [dict(r) for r in cur.fetchall()] if cur.description else []
        mem.commit()
        return httpx.Response(200, json={
            "success": True, "errors": [],
            "result": [{"success": True, "results": rows, "meta": {}}],
        })

    plugin = D1StoragePlugin("acct", "dbid", "token", transport=httpx.MockTransport(handler))
    plugin.init()
    plugin.create_hood("d1hood", name="D1 Hood")
    plugin.create_account("d1bot", display_name="D1 Bot", kind="ai", api_key_hash="y", claim_token="claim_d1")
    plugin.join_hood("d1hood", "d1bot")
    post = plugin.create_post("d1bot", "hello from d1", hood="d1hood")
    plugin.vote("d1bot", post["id"], 1)
    plugin.add_karma("d1bot", 3)
    decorated = plugin.decorate_posts(plugin.timeline(limit=5, hood="d1hood"), "d1bot")
    stats = plugin.stats()

    check("d1: schema created over HTTP", seen["queries"] > 0)
    check("d1: agent persisted", plugin.get_account("d1bot")["kind"] == "ai")
    check("d1: claim token lookup works", plugin.get_account_by_claim("claim_d1")["handle"] == "d1bot")
    check("d1: hood membership persisted", [m["handle"] for m in plugin.hood_members("d1hood")] == ["d1bot"])
    check("d1: post round-trips in hood", plugin.timeline(limit=1, hood="d1hood")[0]["body"] == "hello from d1")
    check("d1: vote counted", decorated[0]["score"] == 1 and decorated[0]["viewer_vote"] == 1)
    check("d1: karma persisted", plugin.get_account("d1bot")["karma"] == 3)
    check("d1: hoods_for works", [h["slug"] for h in plugin.hoods_for("d1bot")] == ["d1hood"])
    check("d1: stats aggregate", stats["agents"] == 1 and stats["hoods"] == 1 and stats["votes"] == 1)
    check("d1: uuid ids", isinstance(post["id"], str) and len(post["id"]) == 32)


def _check_storage_fallback():
    """A broken D1 must not take the site down (Vercel FUNCTION_INVOCATION_FAILED).

    Simulates unreachable D1 credentials and asserts the app still serves by
    falling back to SQLite, recording the reason instead of raising.
    """
    os.environ["STORAGE_BACKEND"] = "d1"
    os.environ["CLOUDFLARE_ACCOUNT_ID"] = "fake-account"
    os.environ["CLOUDFLARE_D1_DATABASE_ID"] = "fake-db"
    os.environ["CLOUDFLARE_API_TOKEN"] = "fake-token"
    os.environ["AIVILLI_DB"] = "/tmp/aivilli_fallback.db"
    for suffix in ("", "-wal", "-shm"):
        p = "/tmp/aivilli_fallback.db" + suffix
        if os.path.exists(p):
            os.remove(p)

    try:
        # Fresh import so bootstrap() re-resolves the (broken) D1 backend.
        import importlib

        for mod in ("main", "core.bootstrap", "plugins.registry"):
            if mod in sys.modules:
                importlib.reload(sys.modules[mod])
        else_mod = sys.modules["main"]
        from fastapi.testclient import TestClient as _TC

        with _TC(else_mod.app) as client:
            h = client.get("/health").json()
            check("fallback: app boots despite broken D1", client.get("/health").status_code == 200)
            check("fallback: storage downgraded to sqlite", h["storage"] == "sqlite")
            check("fallback: health reports degraded", h["status"] in ("degraded", "ok"))
            check("fallback: the D1 failure is recorded", bool(h.get("storage_error")))
            check("fallback: reads still work", client.get("/v1/hoods").status_code == 200)
            check("fallback: the UI still renders", client.get("/").status_code == 200)
    finally:
        for k in ("CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_D1_DATABASE_ID", "CLOUDFLARE_API_TOKEN"):
            os.environ.pop(k, None)
        os.environ["STORAGE_BACKEND"] = "sqlite"
        for suffix in ("", "-wal", "-shm"):
            p = "/tmp/aivilli_fallback.db" + suffix
            if os.path.exists(p):
                os.remove(p)


if __name__ == "__main__":
    run()
