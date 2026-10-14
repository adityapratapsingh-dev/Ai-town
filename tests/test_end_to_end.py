"""End-to-end checks for the AI Town Square API (runs offline).

Uses FastAPI's TestClient against a throwaway SQLite file so the real app is
exercised: schema creation, seeding, the autonomous pulse loop, the human
nudge, the spectator feed and the dashboard route.
"""

import importlib
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TEST_DB = "/tmp/ai_society_test.db"
os.environ["AI_SOCIETY_DB"] = TEST_DB
# Force the offline simulation path so tests never hit the network.
for key in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GROQ_API_KEY"):
    os.environ.pop(key, None)

if os.path.exists(TEST_DB):
    os.remove(TEST_DB)

import database  # noqa: E402
import main  # noqa: E402
from agents import AGENT_NAMES  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

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


def run():
    with TestClient(main.app) as client:
        # --- health ---
        r = client.get("/health")
        check("GET /health -> 200", r.status_code == 200)
        body = r.json()
        check("health status ok", body.get("status") == "ok")
        check("health reports 3 agents", body.get("agents") == 3)
        check("health provider simulation", body.get("provider") == "simulation")

        # --- seeding ---
        agents = database.list_agents()
        check("seeded 3 citizens", len(agents) == 3)
        names = [a["name"] for a in agents]
        check("citizens are Atlas/Sita/Kabir", names == AGENT_NAMES)
        check(
            "roles seeded",
            {a["name"]: a["role"] for a in agents}
            == {
                "Atlas": "Visionary Architect",
                "Sita": "Rigorous Researcher",
                "Kabir": "Pragmatic Coder",
            },
        )

        # --- dashboard ---
        r = client.get("/")
        check("GET / -> 200", r.status_code == 200)
        check("dashboard is html", "text/html" in r.headers.get("content-type", ""))
        check("dashboard has nudge box", "Nudge the society" in r.text)
        check("dashboard polls feed", "/v1/society/feed" in r.text)

        # --- empty feed ---
        r = client.get("/v1/society/feed")
        check("GET /feed -> 200", r.status_code == 200)
        check("feed starts empty", r.json()["messages"] == [])
        check("feed lists agents", len(r.json()["agents"]) == 3)

        # --- pulse (autonomous round, chained) ---
        r = client.post("/v1/society/pulse?steps=3")
        check("POST /pulse -> 200", r.status_code == 200)
        pulse = r.json()
        check("pulse ok", pulse.get("ok") is True)
        check("pulse produced 3 messages", len(pulse["messages"]) == 3)
        speakers = [m["agent_name"] for m in pulse["messages"]]
        check("pulse chains in round-robin", speakers == AGENT_NAMES)
        check("pulse is simulation offline", pulse["provider"] == "simulation")

        # --- feed now populated, newest first ---
        r = client.get("/v1/society/feed")
        msgs = r.json()["messages"]
        check("feed has 3 messages", len(msgs) == 3)
        check("feed newest first", msgs[0]["id"] > msgs[-1]["id"])
        check("messages carry a timestamp", all(m["timestamp"] for m in msgs))

        # --- pulse chains from the last speaker ---
        r = client.post("/v1/society/pulse")
        next_speaker = r.json()["messages"][0]["agent_name"]
        check("next pulse continues rotation", next_speaker == AGENT_NAMES[0])

        # --- nudge ---
        r = client.post(
            "/v1/society/nudge",
            json={"topic": "Should we colonise Mars?", "replies": 2},
        )
        check("POST /nudge -> 200", r.status_code == 200)
        nudge = r.json()
        check("nudge echoes topic", nudge.get("topic") == "Should we colonise Mars?")
        check("nudge produced 2 replies", len(nudge["messages"]) == 2)
        recent3 = database.get_feed(3)  # newest-first: [reply, reply, Human]
        check(
            "human nudge stored before replies",
            [m["agent_name"] for m in recent3] == ["Sita", "Atlas", "Human"],
        )
        check(
            "agents answer the human",
            all(
                "Mars" in m["message"] or "colonise" in m["message"]
                for m in nudge["messages"]
            ),
        )

        # --- nudge validation ---
        r = client.post("/v1/society/nudge", json={"topic": ""})
        check("empty nudge rejected (422)", r.status_code == 422)

        # --- status returns to active after thinking ---
        check(
            "no citizen stuck thinking",
            all(a["status"] == "active" for a in database.list_agents()),
        )

    print(f"\n{checks - len(failures)}/{checks} checks passed")
    if failures:
        print("FAILURES:")
        for f in failures:
            print(" -", f)
        sys.exit(1)


if __name__ == "__main__":
    run()
