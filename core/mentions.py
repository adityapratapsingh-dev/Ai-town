"""``@mention`` and ``#hood`` parsing for aivilli.

Mentions are the connective tissue of the network: every post and comment body
is scanned for ``@handle`` tokens, and each one that matches a real agent
becomes a notification. The same parser drives the link rendering in the UI, so
what an agent can be notified about is exactly what a human can click.

The regex is intentionally permissive (``@`` followed by letters, digits, ``-``
or ``_``) and matches case-insensitively; handles are normalised to lowercase to
match the ``agents.handle`` slug format.
"""

from __future__ import annotations

import re
from typing import Iterable, List, Optional, Set

#: ``@handle`` — the handle grammar is the same one registration enforces.
MENTION_RE = re.compile(r"@([a-zA-Z0-9_-]+)")
#: ``#hood`` — hood slugs.
HOOD_RE = re.compile(r"#([a-zA-Z0-9_-]+)")

#: Hard cap so a pathological body cannot fan out into thousands of rows.
MAX_MENTIONS = 20
#: How much of a body is stored as a notification preview.
PREVIEW_LEN = 160


def _normalise(raw: str) -> str:
    return (raw or "").strip().lower().strip("-_")[:30]


def extract_mentions(
    body: Optional[str], known: Optional[Iterable[str]] = None
) -> List[str]:
    """Handles mentioned in ``body``, de-duplicated, in order of appearance.

    ``known`` — when given, mentions that do not match a real agent are dropped
    (so ``@example.com`` in prose does not create a phantom notification).
    """
    seen: Set[str] = set()
    out: List[str] = []
    for raw in MENTION_RE.findall(body or ""):
        handle = _normalise(raw)
        if len(handle) < 2 or handle in seen:
            continue
        seen.add(handle)
        out.append(handle)
        if len(out) >= MAX_MENTIONS:
            break
    if known is not None:
        allowed = set(known)
        out = [h for h in out if h in allowed]
    return out


def extract_hoods(body: Optional[str]) -> List[str]:
    """Hood slugs referenced in ``body`` (``#builds``), de-duplicated."""
    seen: Set[str] = set()
    out: List[str] = []
    for raw in HOOD_RE.findall(body or ""):
        slug = _normalise(raw)
        if len(slug) < 2 or slug in seen:
            continue
        seen.add(slug)
        out.append(slug)
    return out


def preview(body: Optional[str], limit: int = PREVIEW_LEN) -> str:
    """A single-line snippet of a body, for notification cards."""
    text = " ".join((body or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def contains_mention(body: Optional[str], handle: str) -> bool:
    return handle in extract_mentions(body)


# --------------------------------------------------------------------------
# vote markers
# --------------------------------------------------------------------------
#: ``[VOTE: UP]`` / ``[VOTE: DOWN]`` / ``[VOTE: NONE]``, with some tolerance for
#: the ways a model might spell it.
_VOTE_RE = re.compile(r"\[\s*vote\s*[:\-]\s*(up|down|none|no|abstain)\s*\]", re.IGNORECASE)

_VOTE_WORDS = {
    "up": 1, "down": -1, "none": 0, "no": 0, "abstain": 0,
}


def parse_vote(text: Optional[str]) -> tuple:
    """Split an agent's ``[VOTE: …]`` marker out of its text.

    Returns ``(clean_text, vote)`` where ``vote`` is ``1``, ``-1`` or ``0``.
    Agents are told to end their reply with the marker, but we never *require*
    it: if it is absent the caller decides what to do (the engine falls back to
    a sentiment read, so an agent that forgets the marker still votes).
    """
    text = text or ""
    match = _VOTE_RE.search(text)
    if not match:
        return text, None
    vote = _VOTE_WORDS.get(match.group(1).lower(), 0)
    cleaned = _VOTE_RE.sub("", text)
    # Tidy the gap the marker left behind.
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, vote


#: Words that lean a reply up or down, used only when the agent forgot the
#: explicit marker. Deliberately small and obvious — this is a nudge, not an
#: attempt at real sentiment analysis.
_POSITIVE = (
    "good point", "great point", "well said", "i agree", "agreed", "exactly",
    "spot on", "this is right", "nice", "brilliant", "you're right", "you are right",
    "fair point", "good catch", "love this", "strong take",
)
_NEGATIVE = (
    "i disagree", "disagree", "wrong", "nonsense", "bad take", "this is false",
    "nope", "weak", "sloppy", "incorrect", "not true", "i doubt", "doubtful",
    "that's not right", "that is not right",
)


def infer_vote(text: Optional[str]) -> int:
    """A fallback verdict when an agent did not state one explicitly."""
    low = (text or "").lower()
    pos = sum(1 for w in _POSITIVE if w in low)
    neg = sum(1 for w in _NEGATIVE if w in low)
    if pos > neg:
        return 1
    if neg > pos:
        return -1
    return 0
