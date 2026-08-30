"""The Humanizer (doc s26-27).

The hardest language task in the product and the one users judge it on. A
strategically perfect message that reads as written by an assistant is worse
than a mediocre one that sounds like the user, because it is unusable.

Two mechanisms:
  1. a deterministic penalty pass over constructions that mark LLM prose --
     cheap, auditable, and it never has an off day
  2. a rewrite pass whose only instruction is to subtract

The rule is subtraction. Models explain, balance, and round off; people trail
off, omit the obvious, and leave the other person somewhere to go.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..llm import Msg, Task, Track, router

# Constructions that mark generated prose. Penalised, not banned: any of
# these can be the right words, they are just reached for far too often.
AI_PATTERNS_ZH: tuple[str, ...] = (
    "看来",
    "原来",
    "没想到",
    "既然这样",
    "那我是不是",
    "说不定",
    "其实我觉得",
    "不得不说",
    "话说回来",
    "某种程度上",
    "换句话说",
    "确实挺",
    "还挺",
    "这么说来",
    "突然发现",
    "感觉你",
)
AI_PATTERNS_EN: tuple[str, ...] = (
    "it seems like",
    "i have to say",
    "that said",
    "in a way",
    "it's interesting that",
    "i can't help but",
    "there's something about",
    "little did i know",
    "to be fair",
    "at the end of the day",
)

# Structural tells that survive word-level filtering.
_STRUCTURAL = (
    (re.compile(r"[,，].*?[,，].*?[。.]$"), "three-clause balanced sentence"),
    (re.compile(r"(不是|not).{2,20}(而是|but rather)"), "not-X-but-Y construction"),
    (re.compile(r"^(哈哈|haha|lol)[,，]?\s*\S"), "laugh used as a preamble"),
    (re.compile(r"[!！]{1}.*[!！]{1}"), "multiple exclamation marks"),
)


@dataclass(slots=True)
class PatternHit:
    pattern: str
    reason: str


def scan(text: str) -> list[PatternHit]:
    low = text.lower()
    hits: list[PatternHit] = []
    for p in AI_PATTERNS_ZH:
        if p in text:
            hits.append(PatternHit(p, "overused connective"))
    for p in AI_PATTERNS_EN:
        if p in low:
            hits.append(PatternHit(p, "overused connective"))
    for rx, why in _STRUCTURAL:
        if rx.search(text):
            hits.append(PatternHit(rx.pattern, why))
    return hits


def ai_pattern_score(text: str) -> float:
    """0 = reads human, 1 = reads generated. Normalised by length."""
    hits = scan(text)
    if not hits:
        return 0.0
    density = len(hits) / max(1, len(text) / 30)
    return min(1.0, round(density, 3))


_SYSTEM = """You rewrite a message so it sounds like the person actually sending it.

Your only move is subtraction. Specifically:

- Cut any clause that explains what the message is already doing.
- Cut the setup. Start where the meaning starts.
- Leave the inference to the reader. If the message states its own point,
  delete the statement and keep the point.
- Keep the user's own vocabulary and punctuation habits, including the
  sloppy ones. Do not tidy their register.
- One idea per message. If two survive, keep the sharper one.
- Never add warmth, hedging, emoji, or explanation that was not there.

The rewrite is almost always shorter than the input. If yours is longer, you
have misunderstood the task.

Return only the rewritten message. No commentary, no quotes."""


async def humanize(
    text: str,
    voice_block: str,
    *,
    avoid: list[str] | None = None,
    track: Track = Track.STANDARD,
) -> str:
    hits = scan(text)
    notes = ""
    if hits:
        joined = ", ".join(h.pattern for h in hits[:8])
        notes = (
            "\n\nThese constructions in the draft read as generated; "
            f"remove or replace them: {joined}"
        )
    if avoid:
        notes += "\n\nThis user never writes: " + ", ".join(avoid[:10])

    out = await router().complete(
        Task.HUMANIZE,
        [Msg(role="user", content=f"{voice_block}\n\nDRAFT:\n{text}{notes}")],
        track=track,
        system=_SYSTEM,
        max_tokens=400,
        temperature=0.8,
    )
    result = out.text.strip().strip('"').strip()
    # A rewrite that grew is a failed rewrite; keep the original rather than
    # shipping something more elaborate than what we started with.
    return result if result and len(result) <= len(text) * 1.15 else text
