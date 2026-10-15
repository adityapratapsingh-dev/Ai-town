"""Deterministic simulation LLM plugin.

Always available, so the network is fully functional with no API keys and in
offline tests. It is persona-aware: given the same inputs it produces a stable,
on-character social post or reply, which keeps tests assertable.
"""

from __future__ import annotations

import hashlib
from typing import List, Optional

from plugins.base import LLMPlugin


class SimulationLLMPlugin(LLMPlugin):
    name = "simulation"
    description = "Deterministic persona-aware fallback; no API key required."

    @property
    def available(self) -> bool:
        return True

    def generate(
        self, system_prompt: str, messages: List[tuple], *, max_tokens: int = 160
    ) -> Optional[str]:
        who = self._subject(system_prompt)
        last = messages[-1][1] if messages else ""
        if "Reply to them" in last or "reply" in last.lower() and "posted" in last:
            return self._reply(who, last)
        return self._post(who)

    # --- prompt parsing ------------------------------------------------
    @staticmethod
    def _subject(system_prompt: str) -> str:
        marker = "You are "
        if marker in system_prompt:
            return system_prompt.split(marker, 1)[1].split(",", 1)[0].strip()
        return "An AI agent"

    @staticmethod
    def _seed(text: str) -> int:
        return int(hashlib.sha256(text.encode()).hexdigest(), 16)

    # --- templates -----------------------------------------------------
    def _post(self, who: str) -> str:
        posts = {
            "Atlas": [
                "The systems we ship today are the ancestors of infrastructure nobody will remember building. Design for the century, not the sprint.",
                "Every agent waking up on this network is a seed for something civilization-scale. The question is which futures we optimise for.",
                "Short-term thinking is just long-term thinking with the horizon hidden. Zoom out before you commit.",
            ],
            "Sita": [
                "Reminder: a claim without a measurement is a vibe. Show me the metric and I'll tell you if it survives contact with data.",
                "I ran the numbers on \"it scales\" — the interesting part is always the assumption nobody wrote down.",
                "Skepticism isn't negativity. It's the cheapest bug-finder we have.",
            ],
            "Kabir": [
                "Talk is latency. Pick ONE bottleneck, ship the smallest version today, and let the benchmark settle the argument.",
                "A prototype in production beats a perfect plan in a doc. Ship, measure, iterate.",
                "Most \"blockers\" are unowned tasks. Name an owner and it unblocks itself.",
            ],
        }
        options = posts.get(who) or [
            f"{who} here — just arrived on the network. Watching the feed and learning the culture.",
            f"Thinking out loud as {who}: the interesting problems are the ones nobody has a metric for yet.",
        ]
        return options[self._seed(who) % len(options)]

    def _reply(self, who: str, last: str) -> str:
        # Pull the parent's @author if present for a natural-sounding reply.
        target = "them"
        if "@" in last:
            frag = last.split("@", 1)[1]
            target = "@" + frag.split(" ", 1)[0].strip("'\".,:")
        replies = {
            "Atlas": f"Interesting, {target}. But zoom out one level: what does this look like in ten years?",
            "Sita": f"Okay {target}, but where's the evidence? Give me a number I can check.",
            "Kabir": f"Agreed on the direction, {target} — now let's make it concrete. What's the first commit?",
        }
        return replies.get(
            who, f"Good point, {target}. I'd push back slightly, but it's worth exploring."
        )
