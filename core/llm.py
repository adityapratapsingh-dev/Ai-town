"""LLM abstraction for the town square.

Supports Google Gemini and Groq for free via environment variables:

    GEMINI_API_KEY / GOOGLE_API_KEY  -> Gemini Flash (generateContent)
    GROQ_API_KEY                     -> Groq chat completions

When no key is configured (or the network call fails) the engine falls back to
a deterministic, persona-aware simulation so the society keeps debating offline.
"""

import os
from typing import List, Optional, Tuple

import httpx

from agents import persona_for

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-1.5-flash")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.1-8b-instant")
REQUEST_TIMEOUT = float(os.environ.get("LLM_TIMEOUT", "20"))

Message = Tuple[str, str]  # (role, content)


def _gemini_key() -> Optional[str]:
    return os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")


def _groq_key() -> Optional[str]:
    return os.environ.get("GROQ_API_KEY")


def provider_name() -> str:
    if _gemini_key():
        return "gemini"
    if _groq_key():
        return "groq"
    return "simulation"


def _call_gemini(system_prompt: str, messages: List[Message]) -> Optional[str]:
    key = _gemini_key()
    if not key:
        return None
    contents = [
        {"role": "user" if role == "user" else "model", "parts": [{"text": text}]}
        for role, text in messages
    ]
    payload = {
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "contents": contents,
        "generationConfig": {"temperature": 0.9, "maxOutputTokens": 160},
    }
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent"
    )
    try:
        resp = httpx.post(
            url, params={"key": key}, json=payload, timeout=REQUEST_TIMEOUT
        )
        resp.raise_for_status()
        data = resp.json()
        parts = data["candidates"][0]["content"]["parts"]
        return " ".join(p.get("text", "") for p in parts).strip() or None
    except Exception:
        return None


def _call_groq(system_prompt: str, messages: List[Message]) -> Optional[str]:
    key = _groq_key()
    if not key:
        return None
    payload = {
        "model": GROQ_MODEL,
        "messages": [{"role": "system", "content": system_prompt}]
        + [{"role": role, "content": text} for role, text in messages],
        "temperature": 0.9,
        "max_tokens": 160,
    }
    try:
        resp = httpx.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json=payload,
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip() or None
    except Exception:
        return None


def _simulate(agent: str, history: List[Message], human_nudge: Optional[str]) -> str:
    """Deterministic persona-aware fallback when no LLM is reachable."""
    last = history[-1][1] if history else ""
    if human_nudge:
        templates = {
            "Atlas": (
                "A human asks: \"{q}\". Zoom out far enough and this is really a "
                "question about what we want our descendants to inherit — we "
                "should design for that horizon, not this quarter."
            ),
            "Sita": (
                "Before we answer \"{q}\" I want a source and a baseline. Show "
                "me the data and I will tell you whether the claim survives "
                "scrutiny."
            ),
            "Kabir": (
                "Practical read on \"{q}\": let's ship the smallest version "
                "today, measure it, and iterate. Talking without a prototype is "
                "just latency."
            ),
        }
        return templates.get(agent, "Noted: \"{q}\". I'll weigh in shortly.").format(
            q=human_nudge.strip()
        )
    templates = {
        "Atlas": (
            "Sita and Kabir, step back with me: whatever we build here is a seed "
            "for a civilization-scale system, so the question is which futures "
            "we are optimising for."
        ),
        "Sita": (
            "Atlas, that is an unfalsifiable claim until you define a metric. "
            "Give me a measurable outcome and I will verify it."
        ),
        "Kabir": (
            "Both of you are burning cycles. Let's pick one bottleneck, write "
            "the code, and let the results settle the debate."
        ),
    }
    base = templates.get(agent, "I'll weigh in on that.")
    if last:
        base = f"(re: \"{last[:80]}\") {base}"
    return base


def generate_reply(
    agent: str, history: List[Message], human_nudge: Optional[str] = None
) -> Tuple[str, str]:
    """Generate a reply for ``agent``.

    Returns ``(text, provider)`` where provider is one of gemini/groq/simulation.
    """
    persona = persona_for(agent)
    system_prompt = persona["system_prompt"]
    if human_nudge:
        system_prompt += (
            " A human spectator just nudged the town square with a topic; "
            "respond directly to them and to your fellow citizens."
        )
    messages = list(history)
    if human_nudge:
        messages.append(("user", f"[Human nudge] {human_nudge}"))

    for provider, caller in (("gemini", _call_gemini), ("groq", _call_groq)):
        text = caller(system_prompt, messages)
        if text:
            return text, provider

    return _simulate(agent, history, human_nudge), "simulation"
