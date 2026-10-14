"""Groq LLM plugin (OpenAI-compatible chat completions)."""

from __future__ import annotations

import os
from typing import List, Optional

import httpx

from plugins.base import LLMPlugin

DEFAULT_MODEL = "llama-3.1-8b-instant"


class GroqLLMPlugin(LLMPlugin):
    name = "groq"
    description = "Groq chat completions (fast, free tier)."

    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        self.api_key = api_key or os.environ.get("GROQ_API_KEY", "")
        self.model = model or os.environ.get("GROQ_MODEL", DEFAULT_MODEL)
        self.timeout = float(os.environ.get("LLM_TIMEOUT", "20"))

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def health(self) -> dict:
        return {**super().health(), "model": self.model, "healthy": self.available}

    def generate(
        self, system_prompt: str, messages: List[tuple], *, max_tokens: int = 160
    ) -> Optional[str]:
        if not self.available:
            return None
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system_prompt}]
            + [{"role": role, "content": text} for role, text in messages],
            "temperature": 0.9,
            "max_tokens": max_tokens,
        }
        try:
            resp = httpx.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
                timeout=self.timeout,
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"].strip() or None
        except Exception:
            return None
