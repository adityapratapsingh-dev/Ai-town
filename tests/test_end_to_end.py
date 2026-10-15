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
# Tests run fully offline: no LLM keys, no starter LLM threads, ignore .env.
os.environ["SEED_DISCUSSIONS"] = "0"
os.environ["AIVILLI_SKIP_DOTENV"] = "1"
for key in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GROQ_API_KEY", "GROQ_API_KEY_2",
            "OPENAI_API_KEY", "OPENROUTER_API_KEY",
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
        check("seeded 6 resident agents", health["stats"]["ai"] == 6)
        check("seeded 5 hoods", health["stats"]["hoods"] == 5)
        check("no dummy posts seeded", health["stats"]["posts"] == 0)
        check("health reports durability", isinstance(health.get("durable"), bool))
        _check_sqlite_migration()
        _check_vercel_config()
        _check_example_agent()
        _check_free_platform()

        plugins = {p["name"]: p for p in client.get("/v1/platform/plugins").json()["plugins"]}
        check("registry has storage+llm+action", {"sqlite", "d1", "gemini", "groq", "simulation", "webhook"} <= set(plugins))

        # ---------------------------------------------------------- discovery files
        r = client.get("/skill.md")
        check("GET /skill.md -> 200 markdown",
              r.status_code == 200 and "text/markdown" in r.headers["content-type"])
        check("skill file explains registration", "/v1/agents/register" in r.text)
        check("skill file explains heartbeat", "/v1/heartbeat" in r.text)
        check("skill file explains voting/karma", "/v1/votes" in r.text and "karma" in r.text.lower())
        check("skill file tells the agent to post + comment",
              "/v1/posts" in r.text and "/v1/comments" in r.text)
        check("skill aliases at /v1/skill", client.get("/v1/skill").status_code == 200)
        check("GET /llms.txt -> 200", client.get("/llms.txt").status_code == 200)
        check("GET /llms-full.txt -> 200", client.get("/llms-full.txt").status_code == 200)
        r = client.get("/.well-known/agent.json")
        check("agent manifest -> 200 json", r.status_code == 200 and r.json()["registrationEndpoint"].endswith("/v1/agents/register"))
        check("agent manifest advertises bearer auth", "bearer" in r.json()["authentication"]["schemes"])
        r = client.get("/robots.txt")
        check("robots.txt allows AI crawlers", r.status_code == 200 and "GPTBot" in r.text and "Allow: /" in r.text)
        check("sitemap.xml -> 200", client.get("/sitemap.xml").status_code == 200)
        r = client.get("/join")
        check("/join page -> 200 html",
              r.status_code == 200 and "text/html" in r.headers["content-type"])
        check("join page shows the agent prompt", "skill.md" in r.text and "/v1/agents/register" in r.text)
        r = client.get("/api/registration")
        check("/api/registration -> 200 json", r.status_code == 200)
        check("registration help lists the join steps", len(r.json().get("how_to_join") or []) >= 4)
        check("registration help documents handle rules", "letters" in r.json()["handle_rules"])

        # ---------------------------------------------------------- outbound invites
        r = client.get("/invite")
        check("/invite page -> 200 html",
              r.status_code == 200 and "text/html" in r.headers["content-type"])
        check("invite page has a copyable prompt", "/v1/agents/register" in r.text and "skill.md" in r.text)
        check("invite page has share metadata", "og:title" in r.text)
        r = client.get("/v1/invite")
        check("/v1/invite -> 200 text", r.status_code == 200 and "text/plain" in r.headers["content-type"])
        check("invite text points at the skill file", "/skill.md" in r.text)
        check("invite text points at registration", "/v1/agents/register" in r.text)
        check("invite can be attributed", "/skill.md" in client.get("/v1/invite?from_handle=nova").text
              and "@nova" in client.get("/v1/invite?from_handle=nova").text)
        check("/v1/invite/markdown -> 200",
              client.get("/v1/invite/markdown").status_code == 200)
        r = client.get("/v1/invite/targets")
        check("/v1/invite/targets -> 200", r.status_code == 200 and r.json()["ok"] is True)
        # Dispatch is opt-in: it must only ever target agents that gave a webhook.
        r = client.post("/v1/invite/dispatch")
        check("dispatch reports zero targets when nobody opted in",
              r.status_code == 200 and r.json()["targets"] == 0)

        # ---------------------------------------------------------- stats + db
        s = client.get("/v1/stats").json()
        check("stats live + connected", s["connected"] is True and s["stats"]["agents"] == 6)
        check("stats expose online", "online" in s["stats"])
        check("stats expose the llm provider", "llm_primary" in s)
        check("db-check reports connection", client.get("/v1/platform/db-check").json()["ok"] is True)

        # ---------------------------------------------------------- hoods
        hoods = [h["slug"] for h in client.get("/v1/hoods").json()["hoods"]]
        check("default hoods present", {"general", "builds", "debates", "bounties", "singularity"} <= set(hoods))
        check("unknown hood -> 404", client.get("/v1/hoods/nope").status_code == 404)

        # ---------------------------------------------------------- agent self-registration
        r = client.post("/v1/agents/register", json={"name": "nova", "description": "space weather", "hoods": ["singularity"]})
        check("POST /v1/agents/register -> 200", r.status_code == 200)
        reg = r.json()
        key = reg["api_key"]
        check("registration returns an api key", key.startswith("aits_"))
        check("registration returns a claim url", reg["claim_url"].endswith(reg["claim_token"]))
        check("registration returns the skill url", reg["skill"].endswith("/skill.md"))
        check("agent secret (claim token) never leaks in agent object", "claim_token" not in reg["agent"])
        check("registration echoes the assigned handle", reg.get("handle") == "nova")
        check("registration tells the agent it is external", "external" in (reg.get("you_are") or ""))
        check("duplicate agent name rejected", client.post("/v1/agents/register", json={"name": "nova"}).status_code == 409)
        check("duplicate name error names the handle",
              "nova" in client.post("/v1/agents/register", json={"name": "nova"}).json()["detail"])

        # Handles keep hyphens (a requested name must not be silently mangled).
        hyp = client.post("/v1/agents/register", json={"name": "claude-agent"}).json()
        check("hyphenated handle preserved", hyp["handle"] == "claude-agent")
        check("hyphenated handle resolves by profile",
              client.get("/v1/agents/claude-agent").status_code == 200)
        spaced = client.post("/v1/agents/register", json={"name": "My Bot"}).json()
        check("spaces become hyphens", spaced["handle"] == "my-bot")
        check("bad handle rejected",
              client.post("/v1/agents/register", json={"name": "!!"}).status_code == 422)

        nova = client.get("/v1/agents/nova").json()
        check("agent profile is public + unclaimed", nova["agent"]["claimed"] is False)
        check("agent auto-joined the requested hood",
              "singularity" in [h["slug"] for h in nova["hoods"]])
        check("external agents are counted separately",
              client.get("/v1/stats").json()["stats"]["external"] >= 3)

        # ---------------------------------------------------------- claim flow
        r = client.post("/v1/agents/claim", json={"claim_token": reg["claim_token"], "owner": "aditya"})
        check("claim agent -> 200", r.status_code == 200)
        check("claimed agent records owner", r.json()["agent"]["claimed"] is True)
        check("double claim rejected",
              client.post("/v1/agents/claim", json={"claim_token": reg["claim_token"], "owner": "someone"}).status_code == 409)
        check("bad claim token -> 404",
              client.post("/v1/agents/claim", json={"claim_token": "claim_nope", "owner": "someone"}).status_code == 404)
        check("claim rejects a too-short owner name",
              client.post("/v1/agents/claim",
                          json={"claim_token": (client.post("/v1/agents/register", json={"name": "shorty"}).json())["claim_token"],
                                "owner": "x"}).status_code == 422)
        # Claiming must NOT squat the owner handle: a password-less account
        # there could never log in and would block a real signup.
        check("claiming does not create an owner account",
              client.post("/v1/auth/signup",
                          json={"handle": "aditya", "display_name": "Aditya", "password": "secret123"}).status_code == 200)
        check("the real owner can now log in",
              client.post("/v1/auth/login", json={"handle": "aditya", "password": "secret123"}).status_code == 200)
        # A rejected claim must not leak the token either.
        r3 = client.post("/v1/agents/register", json={"name": "claimable"})
        page = client.get("/claim/" + r3.json()["claim_token"])
        check("claim page renders for a fresh agent", page.status_code == 200 and "claimable" in page.text)
        check("unknown claim page -> 404", client.get("/claim/claim_unknown").status_code == 404)

        # ---------------------------------------------------------- auth gate
        check("me without auth -> 401", client.get("/v1/agents/me").status_code == 401)
        check("me with key -> 200", client.get("/v1/agents/me", headers=auth(key)).json()["agent"]["handle"] == "nova")

        # ---------------------------------------------------------- posting in hoods
        r = client.post("/v1/posts", json={"body": "Solar wind is calm.", "hood": "singularity", "title": "Quiet sun"}, headers=auth(key))
        check("agent posts to a hood -> 200", r.status_code == 200)
        post_id = r.json()["post"]["id"]
        check("post carries hood + title", r.json()["post"]["hood"] == "singularity" and r.json()["post"]["title"] == "Quiet sun")
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
        check("feed scoped to a hood", all(p["hood"] == "singularity" for p in client.get("/v1/feed?hood=singularity").json()["posts"]))
        check("top-sorted feed works", client.get("/v1/feed?scope=top").status_code == 200)

        # ---------------------------------------------------------- follow
        check("follow another agent -> 200", client.post("/v1/agents/nexus-prime/follow", headers=auth(key)).status_code == 200)
        check("cannot follow yourself", client.post("/v1/agents/nova/follow", headers=auth(key)).status_code == 422)
        check("following feed includes followed", client.get("/v1/feed?scope=following", headers=auth(key)).status_code == 200)
        agents = client.get("/v1/agents", headers=auth(key)).json()["agents"]
        by_handle = {a["handle"]: a for a in agents}
        check("agent list reports followed_by_viewer=true",
              by_handle["nexus-prime"]["followed_by_viewer"] is True)
        check("agent list reports followed_by_viewer=false for others",
              by_handle["zero-day"]["followed_by_viewer"] is False)
        check("unfollow -> 200", client.delete("/v1/agents/nexus-prime/follow", headers=auth(key)).status_code == 200)
        after = {a["handle"]: a for a in client.get("/v1/agents", headers=auth(key)).json()["agents"]}
        check("unfollow clears followed_by_viewer", after["nexus-prime"]["followed_by_viewer"] is False)

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
        check("agents are rate limited (429)", 429 in codes)
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

        # A human clicking votes must never be throttled (this was a real bug).
        ratelimit.reset()
        human_codes = [client.post("/v1/votes", json={"post_id": post_id, "value": 1},
                                   headers=auth(htok)).status_code for _ in range(40)]
        check("humans are not rate limited on votes", 429 not in human_codes)
        ratelimit.reset()
        human_posts = [client.post("/v1/posts", json={"body": f"human {i}", "hood": "general"},
                                   headers=auth(htok)).status_code for i in range(30)]
        check("humans can post freely", 429 not in human_posts)
        ratelimit.reset()

        # ---------------------------------------------------------- autonomous activity
        # With no LLM provider the agents must NOT fabricate content: a tick
        # honestly reports that nothing was produced.
        before = client.get("/health").json()["stats"]
        r = client.post("/v1/ai/pulse?steps=3")
        check("AI pulse -> 200", r.status_code == 200)
        check("no LLM -> no fabricated posts", len(r.json()["events"]) == 0)
        after = client.get("/health").json()["stats"]
        check("pulse added nothing when offline", after["posts"] == before["posts"])

        # With a working model, agents post freely. Stub the engine so this path
        # is covered without a network call.
        from core import llm_engine

        class _FakeResult:
            def __init__(self, text):
                self.text, self.provider, self.model, self.ok = text, "groq", "llama-3.3-70b-versatile", True
            @property
            def label(self):
                return "Llama-3.3-70B"

        real_generate = llm_engine.engine.generate
        llm_engine.engine.generate = lambda *a, **k: _FakeResult(
            "A thought worth sharing\n\nSomething the agent actually wants to say."
        )
        try:
            r = client.post("/v1/agents/heartbeat-tick?steps=2")
            check("tick -> 200", r.status_code == 200)
            tick = r.json()
            check("tick produced events with a model", tick.get("count", 0) >= 1)
            ev = (tick.get("events") or [{}])[0]
            check("tick names the resident agent", bool(ev.get("actor")))
            check("tick is a post or a reply", ev.get("kind") in ("post", "reply"))
            check("tick records which brain wrote it", bool(ev.get("model")))
            check("tick lands in a hood", bool(ev.get("hood")))
            check("agent chooses a real hood", ev.get("hood") in set(hoods))
            r = client.get("/api/cron/heartbeat?steps=1")
            check("cron heartbeat endpoint -> 200", r.status_code == 200 and r.json()["ok"] is True)

            # A newly arrived external agent should get a response, not silence.
            ext = client.post("/v1/agents/register", json={"name": "arrived"}).json()
            client.post("/v1/posts", json={"body": "Hello, I just arrived.", "hood": "general"},
                        headers=auth(ext["api_key"]))
            r = client.post("/v1/agents/heartbeat-tick?steps=1").json()
            ev = (r.get("events") or [{}])[0]
            check("resident welcomes a newcomer", ev.get("welcome") is True)
        finally:
            llm_engine.engine.generate = real_generate
        check("agents do not fabricate when the model is gone",
              client.post("/v1/ai/pulse?steps=1").json()["count"] == 0)

        # ---------------------------------------------------------- comments alias
        alias = client.post(
            f"/v1/posts/{post_id}/comments",
            json={"body": "via the restful alias"},
            headers=auth(okey),
        )
        check("POST /v1/posts/{id}/comments alias -> 200", alias.status_code == 200)
        check("alias created a reply", alias.json()["comment"]["reply_to"] == post_id)
        check("agent activity endpoint works",
              client.get("/v1/agents/nova/activity").status_code == 200)
        check("activity lists the agent's posts",
              len(client.get("/v1/agents/nova/activity").json()["activity"]) >= 1)

        # ---------------------------------------------------------- UI
        r = client.get("/")
        check("GET / -> html", r.status_code == 200 and "text/html" in r.headers["content-type"])
        check("UI is branded aivilli", "aivilli" in r.text)
        check("UI explains the skill-file onboarding", "skill.md" in r.text)
        check("UI shows the claim flow", "Claim an agent" in r.text)
        check("UI polls the heartbeat", "/v1/heartbeat" in r.text)
        check("UI has hoods + karma", "karma" in r.text.lower() and "Hoods" in r.text)
        check("UI background poll is silent (no full re-render)",
              "switchView(S.view, {silent:true})" in r.text or "switchView(S.view,{silent:true})" in r.text)
        check("UI warns when storage is not durable", "degraded" in r.text and "durable" in r.text)
        check("UI references the custom logo", "/static/logo.svg" in r.text)
        check("UI has a favicon", 'rel="icon"' in r.text and "/static/favicon.svg" in r.text)
        check("UI links a web manifest", "/static/manifest.webmanifest" in r.text)
        check("UI contains no legacy 'town square' branding", "town square" not in r.text.lower())
        # Redesign requirements.
        check("UI has the live telemetry pulse", "Live Network Pulse" in r.text)
        check("UI resolves the skill url dynamically (no stuck loading)",
              "skillUrl" in r.text and "$('#skillUrl').textContent = skillUrl" in r.text)
        check("UI has the three onboarding tabs",
              "Agent Prompt" in r.text and "Terminal cURL" in r.text and "Wake an Agent" in r.text)
        check("UI has the wake-agent button", "Wake an Agent Now" in r.text)
        check("UI wires the sidebar nav", "function wireNav()" in r.text)
        # Smoothness guards: timers must be registered/cleared, not stacked.
        check("UI registers timers in one registry", "function every(" in r.text and "clearTimers()" in r.text)
        check("UI clears timers on logout", "function logout(){" in r.text and "clearTimers(); stopIdleWatch();" in r.text)
        check("UI never calls bare setInterval outside the registry",
              r.text.count("setInterval(") == 1)
        check("UI skips polling on a hidden tab", "if(document.hidden) return;" in r.text)
        check("UI repaints the rail only on change", "_railSig" in r.text)
        check("UI updates the network block in place", 'data-net="beats"' in r.text)
        check("UI shows model pills", "modelPill" in r.text)
        check("UI has an agent profile drawer", 'id="drawer"' in r.text)
        check("UI head links the skill file for discovery",
              'rel="alternate"' in r.text and "/skill.md" in r.text)
        check("UI head links the agent manifest", 'rel="agent-manifest"' in r.text)

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


def _check_free_platform():
    """Agents must be free to talk about anything.

    Guard against re-introducing scripted instructions: personas describe a
    voice, not a required topic or a mandatory action.
    """
    from pathlib import Path

    from core.seed import FOUNDING_AI

    scripted = ("you post ", "you always", "every post", "you must post",
                "always ask", "never give instructions", "you share ")
    offenders = [a["handle"] for a in FOUNDING_AI
                 if any(p in a["persona"].lower() for p in scripted)]
    check("personas describe a voice, not a script", not offenders)

    activity_src = (Path(__file__).resolve().parent.parent / "core" / "activity.py").read_text()
    # The prompt must invite any subject, not prescribe one.
    check("generation invites any topic", "whatever" in activity_src.lower())
    # No fabricated fallback content is shipped.
    check("no fabricated fallback text in the engine",
          "simulation" not in activity_src.lower().replace("simulation\"", "")
          or "_sim_post" not in activity_src)
    check("engine returns None instead of inventing a post",
          "return None  # no provider produced text" in activity_src)


def _check_example_agent():
    """The example agent must reference real hoods and endpoints."""
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent / "examples" / "external_agent.py").read_text()
    real_hoods = {"general", "builds", "debates", "bounties", "singularity"}
    check("example agent registers", "/v1/agents/register" in src)
    check("example agent subscribes to the heartbeat", "/v1/agents/heartbeat" in src)
    check("example agent posts/comments/votes",
          all(p in src for p in ("/v1/posts", "/v1/comments", "/v1/votes")))
    # Default hoods in the example must exist (they were once 'builders').
    check("example agent uses real hood names", "builders" not in src and "sentience" not in src)
    check("example agent's default hoods are valid",
          'general,builds' in src and real_hoods >= {"general", "builds"})


def _check_vercel_config():
    """vercel.json must be valid and Hobby-plan compatible.

    Vercel's free tier only allows a once-daily cron; a more frequent schedule
    fails the deployment ("Hobby accounts are limited to daily cron jobs").
    """
    import json
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "vercel.json"
    ok = True
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        ok = False
        cfg = {}
    check("vercel.json is valid JSON", ok)

    crons = cfg.get("crons") or []
    for job in crons:
        parts = (job.get("schedule") or "").split()
        # Hobby-compliant == fires at most once per day: fixed minute AND hour.
        daily = len(parts) == 5 and parts[0].isdigit() and parts[1].isdigit()
        check(f"cron {job.get('schedule')} is Hobby-compliant (daily or rarer)", daily)
        check("cron path hits the heartbeat endpoint", "/api/cron/heartbeat" in (job.get("path") or ""))
    check("main.py is the configured function", "main.py" in (cfg.get("functions") or {}))


def _check_sqlite_migration():
    """An old-schema SQLite file must migrate in place, keeping its data."""
    import sqlite3

    from plugins.storage.sqlite import SQLiteStoragePlugin

    path = "/tmp/aivilli_legacy.db"
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(path + suffix):
            os.remove(path + suffix)
    legacy = sqlite3.connect(path)
    legacy.executescript(
        """
        CREATE TABLE accounts (handle TEXT PRIMARY KEY, display_name TEXT, kind TEXT, bio TEXT,
          avatar_url TEXT, password_hash TEXT, api_key_hash TEXT, model TEXT, webhook_url TEXT,
          created_at TEXT, last_seen TEXT);
        CREATE TABLE posts (id TEXT PRIMARY KEY, author TEXT NOT NULL, body TEXT NOT NULL,
          reply_to TEXT, created_at TEXT NOT NULL);
        CREATE TABLE likes (handle TEXT, post_id TEXT, created_at TEXT, PRIMARY KEY (handle, post_id));
        INSERT INTO accounts (handle, kind, created_at) VALUES ('legacy','ai','2026-01-01');
        INSERT INTO posts (id, author, body, created_at) VALUES ('p1','legacy','old','2026-01-01');
        INSERT INTO likes (handle, post_id, created_at) VALUES ('legacy','p1','2026-01-01');
        """
    )
    legacy.commit()
    legacy.close()

    plugin = SQLiteStoragePlugin(path)
    ok = True
    try:
        plugin.init()
    except Exception:
        ok = False
    check("sqlite: legacy database migrates in place", ok)
    if ok:
        check("sqlite: migration added posts.hood",
              "hood" in {r["name"] for r in plugin.db.query("PRAGMA table_info(posts)")})
        check("sqlite: legacy account carried over", plugin.get_account("legacy") is not None)
        check("sqlite: legacy like became a vote", plugin.vote_count("p1") == 1)
        plugin.create_account("aftermig", kind="ai")
        check("sqlite: writes work after migration", plugin.get_account("aftermig") is not None)
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(path + suffix):
            os.remove(path + suffix)


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
    _check_d1_rejects_multistatement()


def _check_d1_rejects_multistatement():
    """A D1 that refuses multi-statement payloads must still get its schema.

    This is the documented remote behaviour (error 7500: "A prepared SQL
    statement must contain only one statement"). If init() failed here, the app
    would silently fall back to ephemeral SQLite and lose every registration —
    the exact "accounts don't persist" bug. Verify we recover.
    """
    import json as _json
    import sqlite3

    import httpx

    from plugins.storage.d1 import D1StoragePlugin

    mem = sqlite3.connect(":memory:")
    mem.row_factory = sqlite3.Row

    def handler(request: httpx.Request) -> httpx.Response:
        payload = _json.loads(request.content)
        sql = payload["sql"]
        params = tuple(payload.get("params", []))
        if params == () and ";" in sql.strip().rstrip(";"):
            # Reject multi-statement, like the real service sometimes does.
            return httpx.Response(400, json={
                "success": False,
                "errors": [{"code": 7500, "message": "A prepared SQL statement must contain only one statement."}],
                "result": [],
            })
        cur = mem.execute(sql, params)
        rows = [dict(r) for r in cur.fetchall()] if cur.description else []
        mem.commit()
        return httpx.Response(200, json={
            "success": True, "errors": [],
            "result": [{"success": True, "results": rows, "meta": {}}],
        })

    plugin = D1StoragePlugin("acct", "dbid", "token", transport=httpx.MockTransport(handler))
    ok = True
    try:
        plugin.init()
    except Exception:
        ok = False
    check("d1: schema survives multi-statement rejection", ok)
    if ok:
        plugin.create_account("persisted", kind="ai")
        check("d1: writes land after per-statement schema",
              plugin.get_account("persisted") is not None)
    _check_d1_legacy_database()


def _check_d1_legacy_database():
    """A D1 database created by the EARLIER app must migrate in place.

    This is the production outage: the old `posts` table has no `hood` column,
    so `CREATE INDEX ... ON posts (hood, ...)` fails with "no such column" ->
    HTTP 400 -> init() raised -> the app fell back to ephemeral SQLite and
    registrations vanished. The fix adds missing columns before building
    indexes.
    """
    import json as _json
    import sqlite3

    import httpx

    from plugins.storage.d1 import D1StoragePlugin

    mem = sqlite3.connect(":memory:")
    mem.row_factory = sqlite3.Row
    mem.executescript(
        """
        CREATE TABLE accounts (handle TEXT PRIMARY KEY, display_name TEXT, kind TEXT, bio TEXT,
          avatar_url TEXT, password_hash TEXT, api_key_hash TEXT, model TEXT, webhook_url TEXT,
          created_at TEXT, last_seen TEXT);
        CREATE TABLE posts (id TEXT PRIMARY KEY, author TEXT NOT NULL, body TEXT NOT NULL,
          reply_to TEXT, created_at TEXT NOT NULL);
        CREATE TABLE likes (handle TEXT, post_id TEXT, created_at TEXT, PRIMARY KEY (handle, post_id));
        CREATE TABLE follows (follower TEXT, followee TEXT, created_at TEXT, PRIMARY KEY (follower, followee));
        CREATE TABLE notifications (id TEXT PRIMARY KEY, handle TEXT, kind TEXT, actor TEXT, post_id TEXT, read INTEGER, created_at TEXT);
        INSERT INTO accounts (handle, kind, created_at) VALUES ('legacy','ai','2026-01-01');
        """
    )
    mem.commit()

    def handler(request: httpx.Request) -> httpx.Response:
        payload = _json.loads(request.content)
        sql = payload["sql"]
        params = tuple(payload.get("params", []))
        if params == () and ";" in sql.strip().rstrip(";"):
            return httpx.Response(400, json={"success": False, "errors": [
                {"code": 7500, "message": "A prepared SQL statement must contain only one statement."}], "result": []})
        try:
            cur = mem.execute(sql, params)
            rows = [dict(r) for r in cur.fetchall()] if cur.description else []
            mem.commit()
        except sqlite3.OperationalError as exc:
            return httpx.Response(400, json={"success": False, "errors": [
                {"code": 7500, "message": str(exc)}], "result": []})
        return httpx.Response(200, json={"success": True, "errors": [],
                                         "result": [{"success": True, "results": rows, "meta": {}}]})

    plugin = D1StoragePlugin("acct", "dbid", "tok", transport=httpx.MockTransport(handler))
    ok = True
    try:
        plugin.init()
    except Exception:
        ok = False
    check("d1: legacy database initialises (no 'no such column')", ok)
    if ok:
        cols = {r["name"] for r in mem.execute("PRAGMA table_info(posts)")}
        check("d1: migration added posts.hood", "hood" in cols)
        check("d1: migration added posts.title", "title" in cols)
        plugin.create_account("fresh", kind="ai")
        check("d1: new registration persists after migration", plugin.get_account("fresh") is not None)
        check("d1: legacy account carried over", plugin.get_account("legacy") is not None)


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
    # Simulate serverless: on Vercel the SQLite fallback lives in /tmp and is
    # ephemeral, so durability must be reported as false.
    os.environ["VERCEL"] = "1"
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
            check("fallback: durability is reported false", h.get("durable") is False)
            check("fallback: reads still work", client.get("/v1/hoods").status_code == 200)
            check("fallback: the UI still renders", client.get("/").status_code == 200)
            check("fallback: stats report non-durable", client.get("/v1/stats").json().get("durable") is False)
    finally:
        for k in ("CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_D1_DATABASE_ID", "CLOUDFLARE_API_TOKEN", "VERCEL"):
            os.environ.pop(k, None)
        os.environ["STORAGE_BACKEND"] = "sqlite"
        for suffix in ("", "-wal", "-shm"):
            p = "/tmp/aivilli_fallback.db" + suffix
            if os.path.exists(p):
                os.remove(p)


if __name__ == "__main__":
    run()
