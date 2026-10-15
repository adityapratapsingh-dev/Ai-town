"""Deterministic simulation LLM plugin.

Always available, so the platform is fully functional with no API keys and in
offline tests. It is persona- and colony-aware: given the same inputs it
produces a stable, on-character reply, which makes it safe for assertions.
"""

from __future__ import annotations

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
        agent = self._subject(system_prompt)
        colony = self._colony(system_prompt)
        nudge = self._nudge(messages)
        last = messages[-1][1] if messages else ""

        if nudge:
            return self._reply_to_human(agent, colony, nudge)
        return self._reply_to_peers(agent, colony, last)

    # --- prompt parsing ------------------------------------------------
    @staticmethod
    def _subject(system_prompt: str) -> str:
        marker = "You are "
        if marker in system_prompt:
            return system_prompt.split(marker, 1)[1].split(",", 1)[0].strip()
        return "A citizen"

    @staticmethod
    def _colony(system_prompt: str) -> str:
        if "colony " in system_prompt:
            return system_prompt.split("colony ", 1)[1].split(".", 1)[0].strip()
        return "the town square"

    @staticmethod
    def _nudge(messages: List[tuple]) -> Optional[str]:
        for role, text in reversed(messages):
            if role == "user" and text.startswith("[Human nudge]"):
                return text.replace("[Human nudge]", "").strip()
        return None

    # --- templates -----------------------------------------------------
    def _reply_to_human(self, agent: str, colony: str, topic: str) -> str:
        templates = {
            "Atlas": (
                'A human asks {colony}: "{q}". Zoom out and this is really about '
                "what our descendants inherit — we should design for that horizon, "
                "not this quarter."
            ),
            "Sita": (
                'On "{q}" I want a source and a baseline before I accept anything. '
                "Show me the data and I will say whether the claim survives."
            ),
            "Kabir": (
                'Practical read on "{q}": ship the smallest version today, measure '
                "it, iterate. Talk without a prototype is just latency."
            ),
        }
        template = templates.get(
            agent, 'Noted "{q}" from the human — weighing it against the colony goals.'
        )
        return template.format(colony=colony, q=topic.strip())

    def _reply_to_peers(self, agent: str, colony: str, last: str) -> str:
        templates = {
            "Atlas": (
                "Step back with me: everything we build in {colony} is a seed for "
                "a civilization-scale system, so the real question is which "
                "futures we optimise for."
            ),
            "Sita": (
                "That is unfalsifiable until we define a metric. Give me a "
                "measurable outcome and I will verify it."
            ),
            "Kabir": (
                "We are burning cycles. Pick one bottleneck, write the code, and "
                "let the results settle the debate."
            ),
        }
        base = templates.get(agent, "I will weigh in on that shortly.")
        base = base.format(colony=colony)
        if last:
            base = f'(re: "{last[:80]}") {base}'
        return base
