"""Google Gemini Flash LLM plugin."""

from __future__ import annotations

import os
from typing import List, Optional

import httpx

from plugins.base import LLMPlugin

DEFAULT_MODEL = "gemini-1.5-flash"


class GeminiLLMPlugin(LLMPlugin):
    name = "gemini"
    description = "Google Gemini Flash via generateContent (free tier)."

    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get(
            "GOOGLE_API_KEY", ""
        )
        self.model = model or os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)
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
        contents = [
            {
                "role": "user" if role == "user" else "model",
                "parts": [{"text": text}],
            }
            for role, text in messages
        ]
        payload = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": contents,
            "generationConfig": {
                "temperature": 0.9,
                "maxOutputTokens": max_tokens,
            },
        }
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.model}:generateContent"
        )
        try:
            resp = httpx.post(
                url, params={"key": self.api_key}, json=payload, timeout=self.timeout
            )
            resp.raise_for_status()
            parts = resp.json()["candidates"][0]["content"]["parts"]
            text = " ".join(p.get("text", "") for p in parts).strip()
            return text or None
        except Exception:
            return None
