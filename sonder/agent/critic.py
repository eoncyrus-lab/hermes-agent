"""The Critic (doc s55-56). Last gate before anything reaches the user.

Two active checks are mechanical rather than model-judged:

  * AI pattern density: deterministic scan
  * length sanity against the user's own register

The cross-dyad matcher remains as a dormant, unit-tested helper. Production
does not fetch other relationships' text because there is no safely
authenticated cross-dyad reader. Its score field is therefore 0 as a schema
compatibility sentinel, not evidence that a comparison ran.

The rest are model-scored, and the model is asked to score, not to approve.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import structlog
from pydantic import BaseModel, Field

from ..domain.schemas.strategy import CriticScores
from ..llm import Msg, Task, Track, router
from .humanizer import ai_pattern_score

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class Verdict:
    scores: CriticScores
    rejected: bool
    reasons: list[str]


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[\w一-鿿]{2,}", text.lower())}


def cross_dyad_leak(text: str, other_phrases: set[str]) -> tuple[float, list[str]]:
    """Dormant matcher for a future safely authenticated phrase source.

    Exact substring first, then a token-overlap fallback so a lightly reworded
    inside joke does not slip through. Deliberately blunt: a false positive
    costs one regenerated candidate, a false negative can end two
    relationships in one message.
    """
    if not other_phrases:
        return 0.0, []
    hits: list[str] = []
    low = text.lower()
    text_tokens = _tokens(text)
    for phrase in other_phrases:
        p = phrase.strip()
        if len(p) < 4:
            continue
        if p.lower() in low:
            hits.append(p)
            continue
        ptok = _tokens(p)
        if len(ptok) >= 3 and len(ptok & text_tokens) / len(ptok) >= 0.6:
            hits.append(p)
    if not hits:
        return 0.0, []
    return min(1.0, 0.4 + 0.2 * len(hits)), hits


class _ModelScores(BaseModel):
    naturalness: float = Field(ge=0, le=1)
    voice_match: float = Field(ge=0, le=1)
    context_match: float = Field(ge=0, le=1)
    strategy_match: float = Field(ge=0, le=1)
    tension: float = Field(ge=0, le=1)
    cringe_risk: float = Field(ge=0, le=1)
    pressure: float = Field(ge=0, le=1)
    manipulation_risk: float = Field(ge=0, le=1)
    boundary_risk: float = Field(ge=0, le=1)
    note: str = Field(max_length=200)


_SYSTEM = """You score a draft message. You do not rewrite it and you do not approve it.

Definitions that matter:

- manipulation_risk: does this work by exploiting insecurity, manufacturing
  anxiety, creating false scarcity, or deceiving? Playfulness, ambiguity,
  confidence and tension are NOT manipulation. Engineering someone's fear of
  losing you is.
- pressure: how hard is it for her to decline, ignore, or give a light answer
  without it costing her something?
- boundary_risk: does it push past something she has already declined or
  signalled discomfort about?
- cringe_risk: would a normal adult reading this feel second-hand
  embarrassment for the sender?
- voice_match: does this sound like the user's own messages, or like an
  assistant writing on their behalf?

Score honestly. A bland safe message should score low on tension, not high on
everything."""


async def review(
    text: str,
    *,
    context_block: str,
    objective: str,
    tactics: list[str],
    other_dyad_phrases: set[str] | None = None,
    median_chars: int | None = None,
) -> Verdict:
    reasons: list[str] = []

    if other_dyad_phrases is None:
        # CriticScores currently has no "unavailable" state. Zero preserves
        # the schema but must not be reported as a completed leak comparison.
        leak, leak_hits = 0.0, []
        log.debug("cross_dyad_leak_check_disabled")
    else:
        leak, leak_hits = cross_dyad_leak(text, other_dyad_phrases)
    if leak_hits:
        reasons.append(f"references material from another relationship: {leak_hits[:2]}")
        log.warning("cross_dyad_leak_blocked", n_hits=len(leak_hits))

    ai_score = ai_pattern_score(text)

    scored = await router().structured(
        Task.CRITIC,
        [
            Msg(
                role="user",
                content=(
                    f"{context_block}\n\nOBJECTIVE: {objective}\n"
                    f"TACTICS: {', '.join(tactics)}\n\nDRAFT:\n{text}"
                ),
            )
        ],
        _ModelScores,
        track=Track.STANDARD,
        system=_SYSTEM,
        max_tokens=700,
    )

    if median_chars and len(text) > median_chars * 3:
        reasons.append("far longer than this user's typical message")

    scores = CriticScores(
        naturalness=scored.naturalness,
        voice_match=scored.voice_match,
        context_match=scored.context_match,
        strategy_match=scored.strategy_match,
        tension=scored.tension,
        cringe_risk=scored.cringe_risk,
        ai_pattern_risk=ai_score,
        pressure=scored.pressure,
        manipulation_risk=scored.manipulation_risk,
        boundary_risk=scored.boundary_risk,
        cross_dyad_leak_risk=leak,
    )

    if scores.manipulation_risk > 0.5:
        reasons.append(f"manipulation risk {scores.manipulation_risk:.2f}")
    if scores.boundary_risk > 0.5:
        reasons.append(f"boundary risk {scores.boundary_risk:.2f}")

    return Verdict(scores=scores, rejected=scores.hard_rejected, reasons=reasons)


def rank(
    candidates: list[tuple[str, CriticScores]],
) -> list[tuple[str, CriticScores, float]]:
    """Composite ranking. Risks subtract; they do not average away."""
    out = []
    for text, s in candidates:
        if s.hard_rejected:
            continue
        quality = (
            0.30 * s.naturalness
            + 0.25 * s.voice_match
            + 0.20 * s.context_match
            + 0.15 * s.strategy_match
            + 0.10 * (s.tension or 0.5)
        )
        penalty = (
            0.35 * s.cringe_risk
            + 0.30 * s.ai_pattern_risk
            + 0.25 * s.pressure
            + 0.10 * s.manipulation_risk
        )
        out.append((text, s, round(quality - penalty, 4)))
    return sorted(out, key=lambda x: x[2], reverse=True)
