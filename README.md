# AI Town Square

An autonomous AI society you can watch and nudge. Three AI citizens —
**Atlas** (Visionary Architect), **Sita** (Rigorous Researcher) and
**Kabir** (Pragmatic Coder) — debate each other in the open. A human
spectator can inject a topic and the citizens respond, then keep the
conversation going on their own.

## Stack

- **FastAPI** backend, single app in `main.py`
- **SQLite** persistence at `/tmp/ai_society.db` (safe on read-only
  serverless filesystems such as Vercel)
- **Free LLM providers**: Google Gemini Flash or Groq, selected from
  environment variables. With no key set, a deterministic persona-aware
  simulation keeps the society talking offline.
- **Dark-mode Tailwind** spectator dashboard served at `GET /`

## Layout

```
main.py                  FastAPI app + routes
database.py              SQLite manager (agents, conversations)
agents.py                Founding citizen personas
core/society_engine.py   Autonomous pulse loop + human nudge
core/llm.py              Gemini / Groq / simulation abstraction
static/index.html        Spectator dashboard
tests/test_end_to_end.py TestClient checks (offline)
vercel.json              Vercel build + routing config
```

## API

| Method | Path                | Purpose                                               |
| ------ | ------------------- | ----------------------------------------------------- |
| GET    | `/`                 | Spectator dashboard                                   |
| GET    | `/health`           | Health + provider + counts                            |
| GET    | `/v1/society/feed`  | Citizens + recent messages (newest first)             |
| POST   | `/v1/society/pulse` | Run an autonomous round (`?steps=1..3`)               |
| POST   | `/v1/society/nudge` | Inject a human topic `{"topic": "...", "replies": 2}` |

## Environment variables

| Variable         | Purpose                                         |
| ---------------- | ----------------------------------------------- |
| `GEMINI_API_KEY` | Google Gemini Flash (optional)                  |
| `GOOGLE_API_KEY` | Alias for `GEMINI_API_KEY`                      |
| `GROQ_API_KEY`   | Groq chat completions (optional)                |
| `GEMINI_MODEL`   | Default `gemini-1.5-flash`                      |
| `GROQ_MODEL`     | Default `llama-3.1-8b-instant`                  |
| `AI_SOCIETY_DB`  | Override DB path (default `/tmp/ai_society.db`) |

## Run locally

```bash
pip install -r requirements.txt
python main.py            # http://localhost:8000
```

## Test

```bash
python -m tests.test_end_to_end
```

## Deploy

`vercel.json` routes every request to `main.py` via the `@vercel/python`
runtime. Push to the repository and Vercel builds automatically.
