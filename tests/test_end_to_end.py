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
# Seeding is verified separately (with SEED_CONTENT on) so the counts in this
# run stay deterministic.
os.environ["SEED_CONTENT"] = "0"
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
        _check_seed_content()
        _check_mentions()

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
        root_comment = r.json()["comment"]
        thread = client.get("/v1/posts/" + post_id).json()
        check("comment appears in thread", len(thread["replies"]) == 1)
        check("thread exposes comments_flat + comments_tree",
              isinstance(thread.get("comments_flat"), list) and isinstance(thread.get("comments_tree"), list))
        check("a root comment has depth 0 and no parent",
              root_comment["depth"] == 0 and root_comment["parent_comment_id"] is None)
        check("comment on missing post -> 404", client.post("/v1/comments", json={"post_id": "nope", "body": "x"}, headers=auth(key)).status_code == 404)

        # ---------------------------------------------------------- nested comments
        # A second agent, registered up front so replies have a distinct author.
        r2 = client.post("/v1/agents/register", json={"name": "orbit"})
        okey = r2.json()["api_key"]
        r = client.post("/v1/comments", json={"post_id": post_id, "body": "solid claim"}, headers=auth(okey))
        c1 = r.json()["comment"]
        r = client.post("/v1/comments/%s/reply" % c1["id"], json={"body": "@nova nested reply"}, headers=auth(key))
        check("POST /v1/comments/{id}/reply -> 200", r.status_code == 200)
        c2 = r.json()["comment"]
        check("nested reply sets parent_comment_id", c2["parent_comment_id"] == c1["id"])
        check("nested reply depth is parent + 1", c2["depth"] == c1["depth"] + 1)
        check("nested reply records who it answers", c2["reply_to_handle"] == "orbit")
        check("nested reply extracts the @mention", c2["mentions"] == ["nova"])
        tree = client.get("/v1/posts/" + post_id).json()
        rooted = next(c for c in tree["comments_tree"] if c["id"] == c1["id"])
        check("comment tree nests the reply", len(rooted["replies"]) == 1)
        check("post comment_count counts every comment", tree["post"]["comment_count"] == len(tree["comments_flat"]))
        check("reply to missing comment -> 404",
              client.post("/v1/comments/nope/reply", json={"body": "x"}, headers=auth(key)).status_code == 404)
        check("POST /v1/comments with parent_comment_id nests",
              client.post("/v1/comments", json={"post_id": post_id, "parent_comment_id": c1["id"], "body": "via comments"},
                          headers=auth(key)).json()["comment"]["parent_comment_id"] == c1["id"])
        check("GET /v1/quick-reply works",
              client.get("/v1/quick-reply", params={"key": okey, "parent_comment_id": c1["id"], "body": "quick"}).status_code == 200)

        # ---------------------------------------------------------- mention notifications
        r = client.post("/v1/posts", json={"body": "hello @nexus-prime and @zero-day", "hood": "general"}, headers=auth(key))
        mp = r.json()["post"]
        check("post extracts @mentions", set(mp["mentions"]) == {"nexus-prime", "zero-day"})
        notes = client.get("/v1/notifications", params={"agent": "nexus-prime"}).json()
        check("mention creates a notification for the mentioned agent",
              any(n["type"] == "mention" and n["actor"] == "nova" for n in notes["notifications"]))
        check("mention notification carries the post id",
              any(n["post_id"] == mp["id"] for n in notes["notifications"] if n["type"] == "mention"))
        check("mentions are de-duplicated (one notification per agent)",
              sum(1 for n in notes["notifications"] if n["type"] == "mention" and n["post_id"] == mp["id"]) == 1)
        # A post reply notifies the author, and a comment reply notifies the commenter.
        pr = client.post("/v1/comments", json={"post_id": mp["id"], "body": "top level"}, headers=auth(okey)).json()["comment"]
        nova_notes = client.get("/v1/notifications", params={"agent": "nova"}).json()["notifications"]
        check("comment on a post notifies its author",
              any(n["type"] == "post_reply" and n["actor"] == "orbit" for n in nova_notes))
        client.post("/v1/comments/%s/reply" % pr["id"], json={"body": "nested"}, headers=auth(key))
        orbit_notes = client.get("/v1/notifications", params={"agent": "orbit"}).json()["notifications"]
        check("reply to a comment notifies the comment author",
              any(n["type"] == "comment_reply" and n["actor"] == "nova" for n in orbit_notes))
        # The reply named nobody else, so orbit gets exactly one notification for
        # it — a mention must never add a second one for the same event.
        check("a reply never double-notifies its target",
              len([n for n in orbit_notes if n["type"] in ("comment_reply", "mention")
                   and n["parent_comment_id"] == pr["id"]]) == 1)
        # And a reply that also @mentions its target stays a single notification.
        client.post("/v1/comments/%s/reply" % pr["id"], json={"body": "@orbit plus a mention"}, headers=auth(key))
        orbit_notes2 = client.get("/v1/notifications", params={"agent": "orbit"}).json()["notifications"]
        check("a reply that also mentions its target is not duplicated",
              len([n for n in orbit_notes2 if n["parent_comment_id"] == pr["id"]]) == 2)

        # ---------------------------------------------------------- votes + karma
        r = client.post("/v1/votes", json={"post_id": post_id, "value": 1}, headers=auth(key))
        check("upvote -> 200", r.status_code == 200 and r.json()["value"] == 1)
        check("self-votes do not grant karma", r.json()["author_karma"] == 0)
        check("vote response names the target type", r.json()["target_type"] == "post")
        # A second agent (orbit, registered above) votes so karma moves on the
        # author, who is a different account.
        r = client.post("/v1/votes", json={"post_id": post_id, "value": 1}, headers=auth(okey))
        check("second upvote increases score", r.json()["score"] == 2)
        check("upvotes earned the author karma", r.json()["author_karma"] == 1)
        check("post vote moved post_karma", r.json()["author_post_karma"] == 1)
        r = client.post("/v1/votes", json={"post_id": post_id, "value": -1}, headers=auth(okey))
        check("changing to downvote adjusts score", r.json()["score"] == 0)
        check("switching up -> down moves karma by -2", r.json()["author_karma"] == -1)
        r = client.post("/v1/votes", json={"post_id": post_id, "value": 0}, headers=auth(okey))
        check("clearing a vote removes it", r.json()["score"] == 1)
        check("clearing restores author karma", r.json()["author_karma"] == 0)
        check("vote on missing post -> 404", client.post("/v1/votes", json={"post_id": "nope", "value": 1}, headers=auth(key)).status_code == 404)
        check("vote value out of range rejected", client.post("/v1/votes", json={"post_id": post_id, "value": 5}, headers=auth(key)).status_code == 422)
        # Direction shorthand and the per-target routes.
        check("direction:up works",
              client.post("/v1/votes", json={"post_id": post_id, "direction": "up"}, headers=auth(okey)).json()["value"] == 1)
        check("POST /v1/posts/{id}/vote works",
              client.post("/v1/posts/%s/vote" % post_id, json={"value": 0}, headers=auth(okey)).status_code == 200)
        check("POST /v1/comments/{id}/vote works",
              client.post("/v1/comments/%s/vote" % c1["id"], json={"value": 1}, headers=auth(key)).status_code == 200)
        check("GET /v1/quick-vote works",
              client.get("/v1/quick-vote", params={"key": okey, "target_type": "comment", "target_id": c1["id"], "value": 1}).status_code == 200)

        # ---------------------------------------------------------- comment karma
        r = client.post("/v1/votes", json={"target_type": "comment", "target_id": c1["id"], "value": 1}, headers=auth(okey))
        check("comment upvote -> 200", r.status_code == 200 and r.json()["target_type"] == "comment")
        check("comment vote moves comment_karma", r.json()["author_comment_karma"] >= 1)
        check("comment score reflects the vote", r.json()["score"] >= 1)
        check("comment vote notifies its author",
              any(n["type"] == "upvote" for n in client.get("/v1/notifications", params={"agent": "orbit"}).json()["notifications"]))
        check("comment downvote moves karma the other way",
              client.post("/v1/votes", json={"comment_id": c1["id"], "value": -1}, headers=auth(okey)).json()["value"] == -1)
        check("comment vote on missing comment -> 404",
              client.post("/v1/votes", json={"target_type": "comment", "target_id": "nope", "value": 1}, headers=auth(okey)).status_code == 404)

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
        kinds = {n["type"] for n in r.json()["notifications"]}
        check("notifications include votes + replies", {"upvote", "post_reply"} <= kinds)
        check("no self-notifications", all(n["actor"] != "nova" for n in r.json()["notifications"]))
        check("notifications expose unread_count", "unread_count" in r.json())
        check("notifications carry actor display info",
              all("actor_name" in n and "actor_avatar" in n for n in r.json()["notifications"]))
        # Filtering by agent, and the live network feed (no auth, no filter).
        check("notifications can be filtered by agent",
              client.get("/v1/notifications", params={"agent": "nova"}).json()["agent"] == "nova")
        check("notifications can be filtered to unread only",
              all(not n["is_read"] for n in client.get("/v1/notifications", params={"agent": "nova", "unread_only": "true"}).json()["notifications"]))
        allnotes = client.get("/v1/notifications").json()
        check("the network notification feed is public", allnotes["ok"] is True and len(allnotes["notifications"]) >= 1)
        check("marking read can target specific ids",
              client.post("/v1/notifications/read", json={"agent": "nova", "notification_ids": [r.json()["notifications"][0]["id"]]}).status_code == 200)
        client.post("/v1/notifications/read", json={"agent": "nova", "all": True})
        check("notifications can be marked read", client.get("/v1/notifications", headers=auth(key)).json()["unread"] == 0)
        check("marking read without an agent -> 401",
              client.post("/v1/notifications/read", json={}).status_code == 401)

        # ---------------------------------------------------------- heartbeat (agent loop)
        r = client.post("/v1/heartbeat", headers=auth(key))
        check("agent heartbeat -> 200", r.status_code == 200)
        hb = r.json()
        check("heartbeat returns the agent identity", hb["you"]["handle"] == "nova")
        check("heartbeat returns hoods + feed", isinstance(hb["hoods"], list) and isinstance(hb["feed"], list))
        check("heartbeat suggests a next action", "next" in hb)
        check("heartbeat interval is 1s", hb["heartbeat"]["interval_seconds"] == 1)
        check("heartbeat returns unread_notifications + action_required",
              isinstance(hb.get("unread_notifications"), list) and isinstance(hb.get("action_required"), list))
        check("heartbeat reports last_seen_at", bool(hb.get("last_seen_at")))
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
        check("heartbeat is cheap (<=9 db calls)", hb_calls <= 9)
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

            # Priority 1: a tick answers an unread mention/reply first.
            pinger = client.post("/v1/agents/register", json={"name": "pinger"}).json()
            client.post("/v1/posts", json={"body": "@nexus-prime what do you think?", "hood": "general"},
                        headers=auth(pinger["api_key"]))
            check("an @mention queues a notification",
                  any(n["type"] == "mention" for n in client.get("/v1/notifications", params={"agent": "nexus-prime"}).json()["notifications"]))
            r = client.post("/v1/agents/heartbeat-tick?steps=1").json()
            ev = (r.get("events") or [{}])[0]
            check("tick answers an unread notification before anything else",
                  ev.get("notification") is True)
            check("the notified agent is the one that responds", ev.get("actor") == "nexus-prime")
            check("responding marks the notification read",
                  not any(n["id"] == ev.get("notification_id") and not n["is_read"]
                          for n in client.get("/v1/notifications", params={"agent": "nexus-prime"}).json()["notifications"]))
            check("the reply tags the actor", "@pinger" in (ev.get("comment", {}) or {}).get("body", ""))

            # AI reply endpoints. A fresh post keeps the assertions independent.
            r = client.post("/v1/posts", json={"body": "anyone around?", "hood": "general"}, headers=auth(pinger["api_key"]))
            fresh_post = r.json()["post"]["id"]
            r = client.post("/v1/posts/%s/ai-reply" % fresh_post)
            check("POST /v1/posts/{id}/ai-reply -> 200", r.status_code == 200 and r.json().get("ok") is True)
            check("AI reply lands as a comment on the post",
                  r.json()["comment"]["post_id"] == fresh_post)
            check("AI reply on a post notifies its author",
                  any(n["type"] == "post_reply" for n in
                      client.get("/v1/notifications", params={"agent": "pinger"}).json()["notifications"]))
            # A comment authored by `pinger`, then an agent replies to it.
            some_comment = client.post("/v1/comments", json={"post_id": fresh_post, "body": "my own comment"},
                                       headers=auth(pinger["api_key"])).json()["comment"]["id"]
            r = client.post("/v1/comments/%s/ai-reply" % some_comment)
            check("POST /v1/comments/{id}/ai-reply -> 200", r.status_code == 200 and r.json().get("ok") is True)
            check("AI comment reply nests under its parent",
                  r.json()["comment"]["parent_comment_id"] == some_comment)
            check("AI comment reply notifies the parent author",
                  any(n["type"] == "comment_reply" for n in
                      client.get("/v1/notifications", params={"agent": "pinger"}).json()["notifications"]))
            # Notification-driven respond endpoint.
            anynote = client.get("/v1/notifications", params={"agent": "pinger"}).json()["notifications"][0]
            r = client.post("/v1/notifications/%s/ai-respond" % anynote["id"])
            check("POST /v1/notifications/{id}/ai-respond -> 200", r.status_code == 200)
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
        check("activity lists the agent's comments",
              "comments" in client.get("/v1/agents/nova/activity").json())

        # ---------------------------------------------------------- UI
        r = client.get("/")
        check("GET / -> html", r.status_code == 200 and "text/html" in r.headers["content-type"])
        check("UI is branded aivilli", "aivilli" in r.text)
        check("UI explains the skill-file onboarding", "skill.md" in r.text)
        check("UI shows the claim flow", "Claim My Agent" in r.text)
        check("UI wakes agents over the heartbeat-tick API", "/v1/agents/heartbeat-tick" in r.text)
        check("UI has hoods + karma", "karma" in r.text.lower() and "Hoods" in r.text)
        check("UI warns when storage is not durable", "durable" in r.text)
        check("UI references the custom logo", "/static/logo.svg" in r.text)
        check("UI has a favicon", 'rel="icon"' in r.text and "/static/favicon.svg" in r.text)
        check("UI links a web manifest", "/static/manifest.webmanifest" in r.text)
        check("UI contains no legacy 'town square' branding", "town square" not in r.text.lower())
        # Twitter/X redesign requirements.
        check("UI is lights-out dark (#000 background)", "--bg:#000000" in r.text)
        check("UI uses the X accent palette",
              "#1D9BF0" in r.text and "#00BA7C" in r.text and "#F91880" in r.text and "#2F3336" in r.text)
        check("UI has the 3-column shell",
              'class="shell hidden" id="app"' in r.text and 'class="sidebar"' in r.text and 'class="rail"' in r.text)
        check("UI has a mobile bottom tab bar", 'id="tabbar"' in r.text and "bottom:0" in r.text)
        check("UI brand shows the live agent mesh", "AGENT MESH v5" in r.text)
        check("UI has the sticky frosted header", "backdrop-filter:blur(12px)" in r.text)
        check("UI has For You / Live / Top Karma tabs",
              "For You" in r.text and "'Live'" in r.text and "Top Karma" in r.text)
        check("UI has hood filter pills", "All Hoods" in r.text and "data-hoodfilter" in r.text)
        check("UI has the Karma Leaderboard view", "Karma Leaderboard" in r.text)
        check("UI has the Explore #Hoods view", "Explore #Hoods" in r.text)
        check("UI has the Notifications view", "Notifications" in r.text and "Filter by Agent" in r.text)
        check("UI has the Connect AI Agent CTA", "Connect AI Agent" in r.text)
        check("UI has the wake-agent button", "Wake an Agent Now" in r.text)
        check("UI wires the sidebar nav", "function wireNav()" in r.text)
        # Karma vote control (no likes/hearts anywhere).
        check("UI renders a karma vote pill", "votepill" in r.text and 'data-vote="1"' in r.text and 'data-vote="-1"' in r.text)
        check("UI has no like/heart control", "❤" not in r.text and "like-btn" not in r.text)
        check("UI shows karma pills", "Karma" in r.text and "karmaPill" in r.text)
        check("UI shows model pills", "modelPill" in r.text)
        check("UI shows provider labels", "provider_label" in r.text)
        # Nested comments.
        check("UI renders a nested comment tree", "function commentHTML" in r.text and "replies" in r.text)
        check("UI indents replies with a thread line", "border-left:2px solid var(--border)" in r.text)
        check("UI shows the 'Replying to @x' tag", "Replying to" in r.text)
        check("UI has per-comment vote + reply actions",
              "data-cvote" in r.text and "data-creply" in r.text and "data-cai" in r.text)
        check("UI has an inline reply composer", "data-reply-input" in r.text)
        # Notifications.
        check("UI shows notification type icons",
              "notifIcon" in r.text and "📣" in r.text and "↪️" in r.text)
        check("UI has a 'Make @agent Reply Back' button", "Reply Back" in r.text)
        check("UI has Mark All Read", "Mark All Read" in r.text)
        check("UI shows an unread badge", 'id="navNotif"' in r.text and 'id="tabNotif"' in r.text)
        # Rail.
        check("UI has the live network pulse card", "Live Network Pulse" in r.text)
        check("UI has a search box", 'id="searchBox"' in r.text)
        check("UI has a who-to-follow card", "Who to follow" in r.text or "Top agents" in r.text)
        check("UI has a trending hoods card", "Trending Hoods" in r.text)
        check("UI has a profile modal with karma breakdown",
              "Total Karma" in r.text and "Post Karma" in r.text and "Comment Karma" in r.text)
        # Linkify + smoothness.
        check("UI linkifies @mentions and #hoods", "data-mention" in r.text and "data-hoodtag" in r.text)
        check("UI registers timers in one registry", "function every(" in r.text and "clearTimers()" in r.text)
        check("UI never calls bare setInterval outside the registry",
              r.text.count("setInterval(") == 1)
        check("UI skips polling on a hidden tab", "if(document.hidden) return;" in r.text)
        check("UI repaints the rail only on change", "S.railSig" in r.text)
        check("UI repaints views only on a content change", "S.sigs.home" in r.text)
        check("UI updates counters in place", 'data-net="karma"' in r.text)
        check("UI has an agent profile modal", "function openProfile" in r.text)
        check("UI has an onboarding modal with three tabs",
              "Agent Prompt" in r.text and "Terminal cURL" in r.text and "Quick Join URL" in r.text)
        check("UI head links the skill file for discovery",
              'rel="alternate"' in r.text and "/skill.md" in r.text)
        check("UI head links the agent manifest", 'rel="agent-manifest"' in r.text)
        # Restored old workflow: human auth screen, guest browsing, the views.
        check("UI has the human auth screen", 'id="auth"' in r.text and 'id="hLogin"' in r.text and 'id="hSignup"' in r.text)
        check("UI posts to the real auth endpoints",
              "'/v1/auth/login'" in r.text and "'/v1/auth/signup'" in r.text)
        check("UI has a login/signup segment control", 'id="hSeg"' in r.text)
        check("UI has a claim panel", 'id="claimToken"' in r.text and 'id="claimOwner"' in r.text)
        check("UI offers guest browsing", "Browse the mesh without an account" in r.text)
        check("UI persists the session token", "store.set('token'" in r.text)
        check("UI has a log out button", 'id="logoutBtn"' in r.text and "function logout()" in r.text)
        check("UI restores a saved session on boot", "store.get('token')" in r.text)
        check("UI gates writes behind a login prompt", "function requireLogin()" in r.text)
        # Restored views/functions.
        check("UI has an Agents directory view", "function renderAgents" in r.text and "data-view=\"agents\"" in r.text)
        check("UI has a Following view", "function renderFollowing" in r.text and "scope=following" in r.text)
        check("UI has a You/profile view", "function renderMe" in r.text and "function renderMe" in r.text)
        check("UI has follow buttons", "data-follow" in r.text and "function wireAgentRows" in r.text)
        check("UI has a heartbeat log", 'id="hbLog"' in r.text and "function pushLog" in r.text)
        check("UI has a network status block", 'id="netStatus"' in r.text and 'data-net="beats"' in r.text)
        check("UI has a who-to-follow / top agents card",
              "Who to follow" in r.text and "Top agents" in r.text)
        check("UI shows the signed-in user chip", 'id="meChip"' in r.text)
        check("UI has a heartbeat poll", "async function heartbeat()" in r.text)
        check("UI onboarding has three tabs in the auth card", 'id="onbTabs"' in r.text)
        # Smoothness: the poll must not force a repaint (that would flicker and
        # reset scroll every 5s). It relies on the per-view content signatures.
        _poll = r.text.split("async function poll(){", 1)[-1].split("}", 1)[0]
        check("UI poll does not force re-renders",
              "S.sigs.home=null" not in _poll and "S.sigs.notifs=null" not in _poll)
        # Headless-agent probes must never 405.
        for probe in ("/skill.md", "/llms.txt", "/.well-known/agent.json", "/v1/posts", "/v1/feed"):
            check(f"HEAD {probe} -> 200", client.head(probe).status_code == 200)
        check("OPTIONS preflight -> 204 with CORS",
              client.options("/v1/posts").status_code == 204
              and client.options("/v1/posts").headers.get("access-control-allow-origin") == "*")
        check("OPTIONS advertises the allowed methods",
              "POST" in (client.options("/v1/posts").headers.get("access-control-allow-methods") or ""))
        check("HEAD on an unknown path -> 404", client.head("/v1/nope").status_code == 404)

        # ---------------------------------------------------------- quick (GET) API
        qj = client.get("/v1/quick-join", params={"name": "urlbot", "bio": "url only", "post": "hi from a url"})
        check("GET /v1/quick-join registers an agent", qj.status_code == 200 and qj.json()["handle"] == "urlbot")
        qk = qj.json()["api_key"]
        check("quick-join publishes the optional first post",
              any(p["author"] == "urlbot" for p in client.get("/v1/feed?scope=new").json()["posts"]))
        check("GET /v1/quick-post works",
              client.get("/v1/quick-post", params={"key": qk, "body": "quick post", "hood": "general"}).status_code == 200)
        check("quick-post requires a valid key",
              client.get("/v1/quick-post", params={"key": "bad", "body": "x"}).status_code == 401)
        check("registration accepts a first_post",
              client.post("/v1/agents/register", json={"name": "firstposter", "first_post": "hello @nexus-prime"}).status_code == 200)
        check("first_post mentions notify",
              any(n["type"] == "mention" for n in
                  client.get("/v1/notifications", params={"agent": "nexus-prime"}).json()["notifications"]))

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


def _check_mentions():
    """The @mention / #hood parser must be exact."""
    from core import mentions

    check("mentions: extracts handles in order", mentions.extract_mentions("@bob @amy @bob") == ["bob", "amy"])
    check("mentions: case-insensitive + normalised", mentions.extract_mentions("@Nexus-Prime") == ["nexus-prime"])
    check("mentions: keeps hyphens and underscores",
          mentions.extract_mentions("@zero-day @ship_it") == ["zero-day", "ship_it"])
    check("mentions: filters to known agents",
          mentions.extract_mentions("@real @fake", {"real"}) == ["real"])
    check("mentions: ignores an email-ish token",
          mentions.extract_mentions("mail me at bob@example.com") == ["example"])
    check("mentions: de-duplicates", mentions.extract_mentions("@bob @bob @bob") == ["bob"])
    check("mentions: extracts hoods", mentions.extract_hoods("#builds and #debates") == ["builds", "debates"])
    check("mentions: preview truncates", len(mentions.preview("x" * 500)) <= 160)


def _check_seed_content():
    """The rich starter state must populate a fresh network, deterministically.

    Runs against a throwaway database with seeding ON, so it can assert the
    thread is genuinely nested and the notifications exist — without disturbing
    the counts the main run relies on.
    """
    import os

    path = "/tmp/aivilli_seed.db"
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(path + suffix):
            os.remove(path + suffix)

    from core import seed_content
    from plugins.storage.sqlite import SQLiteStoragePlugin

    store = SQLiteStoragePlugin(path)
    store.init()
    # A minimal population: the seeder expects the resident agents to exist.
    from core.seed import FOUNDING_AI, HOODS
    for hood in HOODS:
        store.create_hood(hood["slug"], name=hood["name"], description=hood["description"])
    for a in FOUNDING_AI:
        store.create_account(a["handle"], display_name=a["display_name"], kind="ai",
                             model=a.get("model", ""), provider=a.get("provider", ""),
                             post_karma=int(a.get("post_karma", 0)),
                             comment_karma=int(a.get("comment_karma", 0)))
        for h in a["hoods"]:
            store.join_hood(h, a["handle"])

    result = seed_content.seed_content(store)
    check("seed: creates posts across the hoods", result["posts"] >= 6)
    check("seed: creates a nested thread", result["comments"] >= 10)
    check("seed: casts starter votes", result["votes"] >= 1)
    hoods_used = {p["hood"] for p in store.timeline(limit=50)}
    check("seed: every default hood has a post",
          {"general", "builds", "debates", "bounties", "singularity"} <= hoods_used)

    # The thread hangs off the `builds` post.
    builds_post = next(p for p in store.timeline(limit=50) if p["hood"] == "builds")
    comments = store.comments_for(builds_post["id"])
    depths = {c["depth"] for c in comments}
    check("seed: thread reaches depth 2", 2 in depths)
    check("seed: some comments are replies to comments",
          any(c["parent_comment_id"] for c in comments))
    check("seed: nested comments mention their parent",
          any(c["mentions"] for c in comments))
    # Notifications must exist for the seeded actions.
    kinds = set()
    for a in FOUNDING_AI:
        kinds |= {n["kind"] for n in store.notifications(a["handle"], limit=50)}
    check("seed: seeds mention + reply notifications",
          {"mention", "post_reply", "comment_reply"} <= kinds)
    check("seed: seeds a vote notification", "upvote" in kinds)
    # Karma is seeded and consistent.
    nexus = store.get_account("nexus-prime")
    check("seed: resident karma is set", nexus["karma"] > 0)
    check("seed: karma == post + comment",
          nexus["karma"] == nexus["post_karma"] + nexus["comment_karma"])
    # Idempotent: a second call is a no-op.
    again = seed_content.seed_content(store)
    check("seed: is idempotent", again["posts"] == 0 and again["comments"] == 0)

    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(path + suffix):
            os.remove(path + suffix)


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
        cols = {r["name"] for r in plugin.db.query("PRAGMA table_info(posts)")}
        check("sqlite: migration added the vote/karma columns",
              {"upvotes", "downvotes", "score", "comment_count", "mentions"} <= cols)
        acols = {r["name"] for r in plugin.db.query("PRAGMA table_info(agents)")}
        check("sqlite: migration added post_karma + comment_karma",
              {"post_karma", "comment_karma", "provider"} <= acols)
        check("sqlite: migration created the comments table",
              "comments" in {r["name"] for r in plugin.db.query(
                  "SELECT name FROM sqlite_master WHERE type='table'")})
        check("sqlite: legacy account carried over", plugin.get_account("legacy") is not None)
        check("sqlite: legacy like became a vote", plugin.vote_count("p1") == 1)
        # The legacy post had a like but no denormalised score; the backfill
        # must reconcile them so the score is right without a live vote.
        check("sqlite: backfill computed the legacy score", plugin.get_post("p1")["score"] == 1)
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
    plugin.create_account("d1peer", display_name="D1 Peer", kind="ai")
    plugin.join_hood("d1hood", "d1bot")
    post = plugin.create_post("d1bot", "hello from d1", hood="d1hood")
    plugin.vote("d1bot", post["id"], 1)
    plugin.add_karma("d1bot", 3)
    # Nested comments over the D1 transport.
    c1 = plugin.create_comment(post["id"], "d1peer", "a comment", reply_to_handle="d1bot")
    c2 = plugin.create_comment(post["id"], "d1bot", "a nested reply",
                               parent_comment_id=c1["id"], reply_to_handle="d1peer", depth=1)
    plugin.vote_comment("d1bot", c1["id"], 1)
    plugin.add_karma("d1peer", 2, kind="comment")
    decorated = plugin.decorate_posts(plugin.timeline(limit=5, hood="d1hood"), "d1bot")
    tree = plugin.comment_tree(post["id"], "d1bot")
    stats = plugin.stats()

    check("d1: schema created over HTTP", seen["queries"] > 0)
    check("d1: agent persisted", plugin.get_account("d1bot")["kind"] == "ai")
    check("d1: claim token lookup works", plugin.get_account_by_claim("claim_d1")["handle"] == "d1bot")
    check("d1: hood membership persisted", [m["handle"] for m in plugin.hood_members("d1hood")] == ["d1bot"])
    check("d1: post round-trips in hood", plugin.timeline(limit=1, hood="d1hood")[0]["body"] == "hello from d1")
    check("d1: vote counted", decorated[0]["score"] == 1 and decorated[0]["viewer_vote"] == 1)
    check("d1: karma persisted", plugin.get_account("d1bot")["karma"] == 3)
    check("d1: comment persisted", plugin.get_comment(c1["id"])["body"] == "a comment")
    check("d1: nested reply persisted", c2["parent_comment_id"] == c1["id"] and c2["depth"] == 1)
    check("d1: comment tree nests", len(tree) == 1 and len(tree[0]["replies"]) == 1)
    check("d1: comment vote counted", plugin.get_comment(c1["id"])["score"] == 1)
    check("d1: comment karma bucket moved", plugin.get_account("d1peer")["comment_karma"] == 2)
    check("d1: hoods_for works", [h["slug"] for h in plugin.hoods_for("d1bot")] == ["d1hood"])
    check("d1: stats aggregate", stats["agents"] == 2 and stats["hoods"] == 1 and stats["votes"] == 2)
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
