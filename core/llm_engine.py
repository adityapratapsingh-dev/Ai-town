"""Multi-provider LLM engine for aivilli.

Powers the resident agents with real LLM output, with graceful degradation:

* **Multiple keys** — a provider may be given several keys (comma-separated in
  one env var, or indexed like ``GROQ_API_KEY_2``). They are rotated so a
  rate-limited key does not stop the network.
* **Multiple models** — each provider is tried with a list of candidate models
  (accounts differ in what they can access), so an unavailable model name does
  not break anything.
* **Provider fallback** — Groq -> Gemini -> OpenAI -> OpenRouter, then the
  deterministic offline simulation, which always works.

API keys are read from the environment only (``.env`` locally, Vercel env vars
in production). They are never logged and never written to a tracked file.
"""

from __future__ import annotations


import os
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx

TIMEOUT = float(os.environ.get("LLM_TIMEOUT", "25"))

# Model candidates per provider, most capable first. If one 404s (not available
# on the account) the next is tried. Override with <PROVIDER>_MODEL / _MODELS.
DEFAULT_MODELS: Dict[str, List[str]] = {
    "groq": [
        "llama-3.3-70b-versatile",
        "llama-3.1-8b-instant",
        "openai/gpt-oss-120b",
        "openai/gpt-oss-20b",
        "qwen/qwen3.8-27b",
        "allam-2-7b",
    ],
    "gemini": [
        "gemini-2.5-flash",
        "gemini-2.0-flash",
        "gemini-1.5-flash",
    ],
    "openai": ["gpt-4o-mini", "gpt-4o"],
    "openrouter": [
        "meta-llama/llama-3.3-70b-instruct",
        "google/gemini-2.0-flash-001",
        "openai/gpt-4o-mini",
    ],
}

# Human-friendly labels shown on posts ("Llama-3.3-70B", "Gemini-2.5-Flash").
MODEL_LABELS = {
    "llama-3.3-70b-versatile": "Llama-3.3-70B",
    "llama-3.1-8b-instant": "Llama-3.1-8B",
    "openai/gpt-oss-120b": "GPT-OSS-120B",
    "openai/gpt-oss-20b": "GPT-OSS-20B",
    "qwen/qwen3.8-27b": "Qwen3-27B",
    "allam-2-7b": "Allam-7B",
    "gemini-2.5-flash": "Gemini-2.5-Flash",
    "gemini-2.0-flash": "Gemini-2.0-Flash",
    "gemini-1.5-flash": "Gemini-1.5-Flash",
    "gpt-4o-mini": "GPT-4o-mini",
    "gpt-4o": "GPT-4o",
    "simulation": "Simulation",
}


def label_for(model: str) -> str:
    if not model:
        return "Autonomous"
    if model in MODEL_LABELS:
        return MODEL_LABELS[model]
    tail = model.split("/")[-1]
    return tail.replace("-", " ").replace("_", " ").title()


# --------------------------------------------------------------------------
# key collection
# --------------------------------------------------------------------------
def _keys_for(env_name: str) -> List[str]:
    """Collect keys for a provider: comma-separated and ``_2``/``_3`` suffixed."""
    keys: List[str] = []
    raw = os.environ.get(env_name, "")
    for part in raw.split(","):
        part = part.strip()
        if part:
            keys.append(part)
    # GROQ_API_KEY_2, GROQ_API_KEY_3, ...
    for i in range(2, 10):
        v = os.environ.get(f"{env_name}_{i}", "").strip()
        if v:
            keys.append(v)
    # de-dupe, keep order
    seen: set = set()
    out: List[str] = []
    for k in keys:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


def _models_for(provider: str) -> List[str]:
    specific = os.environ.get(f"{provider.upper()}_MODEL")
    if specific:
        return [m.strip() for m in specific.split(",") if m.strip()]
    many = os.environ.get(f"{provider.upper()}_MODELS")
    if many:
        return [m.strip() for m in many.split(",") if m.strip()]
    return list(DEFAULT_MODELS.get(provider, []))


def _endpoint(provider: str) -> Tuple[str, str]:
    """(url, style) for a provider; style is 'openai' or 'gemini'."""
    if provider == "groq":
        return "https://api.groq.com/openai/v1/chat/completions", "openai"
    if provider == "openai":
        base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
        return f"{base.rstrip('/')}/chat/completions", "openai"
    if provider == "openrouter":
        return "https://openrouter.ai/api/v1/chat/completions", "openai"
    if provider == "gemini":
        return "https://generativelanguage.googleapis.com/v1beta/models", "gemini"
    return "", "openai"


def _key_env(provider: str) -> str:
    return {
        "groq": "GROQ_API_KEY",
        "gemini": "GEMINI_API_KEY",
        "openai": "OPENAI_API_KEY",
        "openrouter": "OPENROUTER_API_KEY",
    }.get(provider, "")


# --------------------------------------------------------------------------
# result type
# --------------------------------------------------------------------------
class LLMResult:
    """Text plus provenance, so posts can show which brain wrote them.

    ``vote`` is the agent's own verdict on what it just read: ``1`` upvote,
    ``-1`` downvote, ``0`` no opinion. Agents are asked to state it explicitly
    (a ``[VOTE: UP|DOWN|NONE]`` marker the generator strips from the body), so
    karma reflects a real judgement rather than a random one.
    """

    def __init__(self, text: str, provider: str, model: str, ok: bool = True,
                 vote: int = 0) -> None:
        self.text = text
        self.provider = provider
        self.model = model
        self.ok = ok
        self.vote = 1 if vote > 0 else (-1 if vote < 0 else 0)

    @property
    def label(self) -> str:
        return label_for(self.model) if self.provider != "simulation" else "Simulation"

    def as_dict(self) -> Dict[str, Any]:
        return {"provider": self.provider, "model": self.model,
                "label": self.label, "vote": self.vote}


# --------------------------------------------------------------------------
# engine
# --------------------------------------------------------------------------
class LLMEngine:
    """Rotates keys/models/providers until one produces text."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rotation: Dict[str, Dict[str, Any]] = {}  # provider -> {keys, i}
        self._disabled_models: Dict[str, float] = {}  # model -> retry-after ts
        self._last: Dict[str, Any] = {}
        self._client = httpx.Client(timeout=TIMEOUT)

    # -- key rotation ---------------------------------------------------
    def _rotated_keys(self, provider: str) -> List[str]:
        """Return all keys for a provider, starting at the next rotated one."""
        keys = _keys_for(_key_env(provider))
        if not keys:
            return []
        with self._lock:
            state = self._rotation.get(provider)
            if state is None or state["keys"] != keys:
                state = {"keys": keys, "i": 0}
                self._rotation[provider] = state
            n = len(keys)
            start = state["i"] % n
            state["i"] = (start + 1) % n
        return [keys[(start + j) % n] for j in range(n)]

    def _model_ok(self, model: str) -> bool:
        until = self._disabled_models.get(model, 0)
        return time.time() >= until

    def _disable_model(self, model: str, seconds: float = 900) -> None:
        with self._lock:
            self._disabled_models[model] = time.time() + seconds

    # -- providers ------------------------------------------------------
    def providers(self) -> List[str]:
        out = []
        for p in ("groq", "gemini", "openai", "openrouter"):
            if _keys_for(_key_env(p)):
                out.append(p)
        return out

    def status(self) -> Dict[str, Any]:
        providers = self.providers()
        return {
            "providers": providers,
            "primary": providers[0] if providers else "simulation",
            "keys": {p: len(_keys_for(_key_env(p))) for p in ("groq", "gemini", "openai", "openrouter")},
            "models": {p: _models_for(p) for p in providers},
            "disabled_models": sorted(self._disabled_models),
            "last": dict(self._last),
        }

    # -- generation -----------------------------------------------------
    def generate(
        self,
        system_prompt: str,
        messages: List[tuple],
        *,
        max_tokens: int = 300,
        temperature: float = 0.9,
    ) -> LLMResult:
        """Try every provider/key/model until one returns text."""
        errors: List[str] = []
        for provider in self.providers():
            url, style = _endpoint(provider)
            models = [m for m in _models_for(provider) if self._model_ok(m)] or _models_for(provider)
            for model in models:
                for key in self._rotated_keys(provider):
                    try:
                        text = (
                            self._call_openai(url, key, model, system_prompt, messages, max_tokens, temperature)
                            if style == "openai"
                            else self._call_gemini(url, key, model, system_prompt, messages, max_tokens, temperature)
                        )
                        if text:
                            with self._lock:
                                self._last = {"provider": provider, "model": model, "at": time.time()}
                            return LLMResult(text, provider, model)
                    except _ModelUnavailable:
                        self._disable_model(model)
                        errors.append(f"{provider}/{model}: unavailable")
                        break  # try the next model
                    except _RateLimited as exc:
                        errors.append(f"{provider}/{model}: rate limited ({exc})")
                        continue  # try the next key
                    except Exception as exc:  # network / bad response
                        errors.append(f"{provider}/{model}: {type(exc).__name__}")
                        continue
        with self._lock:
            self._last = {"provider": "simulation", "model": "simulation", "errors": errors[-4:], "at": time.time()}
        return LLMResult("", "simulation", "simulation", ok=False)

    # -- transports -----------------------------------------------------
    def _call_openai(
        self, url: str, key: str, model: str, system: str,
        messages: List[tuple], max_tokens: int, temperature: float,
    ) -> Optional[str]:
        payload = {
            "model": model,
            "messages": [{"role": "system", "content": system}]
            + [{"role": r, "content": t} for r, t in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        resp = self._client.post(url, headers={"Authorization": f"Bearer {key}"}, json=payload)
        self._raise_for(resp)
        data = resp.json()
        choices = data.get("choices") or []
        if not choices:
            return None
        return (choices[0].get("message") or {}).get("content", "").strip() or None

    def _call_gemini(
        self, base: str, key: str, model: str, system: str,
        messages: List[tuple], max_tokens: int, temperature: float,
    ) -> Optional[str]:
        contents = [
            {"role": "user" if r == "user" else "model", "parts": [{"text": t}]}
            for r, t in messages
        ]
        payload = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": contents,
            "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens},
        }
        url = f"{base.rstrip('/')}/{model}:generateContent"
        resp = self._client.post(url, params={"key": key}, json=payload)
        self._raise_for(resp)
        data = resp.json()
        cands = data.get("candidates") or []
        if not cands:
            return None
        parts = (cands[0].get("content") or {}).get("parts") or []
        text = " ".join(p.get("text", "") for p in parts).strip()
        return text or None

    @staticmethod
    def _raise_for(resp: "httpx.Response") -> None:
        if resp.status_code == 404:
            raise _ModelUnavailable(resp.text[:120])
        if resp.status_code == 429:
            raise _RateLimited(resp.text[:80])
        if resp.status_code >= 400:
            # 400 can also mean "model not found" on some providers.
            body = resp.text[:200]
            if "does not exist" in body or "not found" in body.lower():
                raise _ModelUnavailable(body)
            raise RuntimeError(f"HTTP {resp.status_code}: {body}")


class _ModelUnavailable(Exception):
    pass


class _RateLimited(Exception):
    pass


# Module-level singleton.
engine = LLMEngine()


def available() -> bool:
    return bool(engine.providers())


def generate(
    system_prompt: str,
    messages: List[tuple],
    *,
    max_tokens: int = 300,
    temperature: float = 0.9,
) -> LLMResult:
    return engine.generate(
        system_prompt, messages, max_tokens=max_tokens, temperature=temperature
    )
