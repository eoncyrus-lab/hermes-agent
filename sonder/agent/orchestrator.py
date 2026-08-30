"""The orchestrator.

Two paths, deliberately different in shape:

  hot  -- Reply. State and preferences are pre-computed, so this is
          generate -> humanize -> critique -> rank. A few seconds. No agent
          loop, because the user is standing in a chat window waiting.

  slow -- Analysis, review, strategy. Skills are loaded, reasoning is
          multi-step, twenty seconds is fine because the user asked for a
          considered answer.

Conflating the two is the mistake that makes relationship assistants feel
either slow or shallow, depending on which shape won.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum
from uuid import UUID

import structlog

from ..analyzer.features import sha256
from ..domain.schemas.strategy import StrategyRecord, UserAction
from ..llm import Msg, Task, Track, router
from ..storage.repositories import StrategyRepo
from ..storage.scope import Scope
from . import context as ctx_mod
from . import critic, generator, humanizer, skills
from .strategy import Plan
from .strategy import plan as make_plan

log = structlog.get_logger(__name__)
MODEL_VERSION = "orchestrator/1.0.0"


class Intent(str, Enum):
    REPLY = "REPLY"
    ANALYZE_CONVERSATION = "ANALYZE_CONVERSATION"
    RELATIONSHIP_REVIEW = "RELATIONSHIP_REVIEW"
    STRATEGY = "STRATEGY"
    CONFLICT = "CONFLICT"
    SELF_REFLECTION = "SELF_REFLECTION"
    DATE_PLANNING = "DATE_PLANNING"
    COMPATIBILITY = "COMPATIBILITY"
    INTIMACY = "INTIMACY"

    @property
    def is_hot(self) -> bool:
        return self is Intent.REPLY

    @property
    def track(self) -> Track:
        """Which provider family this intent's generation should run on.

        Only generation and rewriting follow the track; signal extraction and
        the Critic never do, and the router refuses to let them.
        """
        return Track.INTIMATE if self is Intent.INTIMACY else Track.STANDARD


@dataclass(slots=True)
class Suggestion:
    text: str
    tactic: str
    register: str
    score: float
    scores: object
    strategy_id: UUID | None = None


@dataclass(slots=True)
class ReplyResult:
    suggestions: list[Suggestion]
    plan: Plan
    health_warning: str | None = None
    rejected_count: int = 0
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class RoutedRequest:
    text: str
    intent: Intent
    track: Track


# Ordered, not a dict: these are substring matches and the specific patterns
# have to be tried first. "我们性生活最近有点问题" contains both "性生活" and
# "最近", and an unordered first-match returns whichever came first by
# accident rather than the one that is actually more specific.
INTENT_HINTS: tuple[tuple[Intent, tuple[str, ...]], ...] = (
    (
        Intent.INTIMACY,
        (
            "性生活",
            "做爱",
            "上床",
            "前戏",
            "高潮",
            "避孕",
            "安全套",
            "性癖",
            "亲密",
            "身体接触",
            "床上",
            "接吻",
            "性",
            "sex",
            "intimacy",
            "foreplay",
            "orgasm",
            "contraception",
        ),
    ),
    (Intent.CONFLICT, ("吵架", "冷战", "闹掰", "和好", "道歉")),
    (Intent.SELF_REFLECTION, ("我是不是", "上头", "我该不该")),
    (Intent.STRATEGY, ("怎么推进", "下一步", "接下来怎么", "策略")),
    (Intent.RELATIONSHIP_REVIEW, ("降温", "最近是不是", "是不是变了", "最近")),
    (Intent.COMPATIBILITY, ("合适吗", "合不合适", "长期")),
    (Intent.DATE_PLANNING, ("约会", "约她", "第一次见")),
    (Intent.ANALYZE_CONVERSATION, ("分析", "什么意思", "怎么理解")),
    (Intent.REPLY, ("怎么回", "回什么", "reply", "该说什么")),
)


def classify(text: str) -> Intent:
    """Cheap keyword routing, specific patterns first.

    Deliberately not a model call: this is on the hot path, and a misroute
    is cheap to correct while a second round-trip is not.
    """
    low = text.lower()
    for intent, hints in INTENT_HINTS:
        if any(h in low for h in hints):
            return intent
    return Intent.REPLY


def route_request(text: str) -> RoutedRequest:
    """Classify one request and apply an exact leading track override.

    `/intimate` is a command only at the beginning and only as a complete
    token. The command is removed before any prompt sees the user's text.
    """
    prefix = "/intimate"
    explicit = text.startswith(prefix) and (
        len(text) == len(prefix) or text[len(prefix)].isspace()
    )
    clean = text[len(prefix) :].lstrip() if explicit else text
    intent = classify(clean)
    track = Track.INTIMATE if explicit else intent.track
    return RoutedRequest(clean, intent, track)


def health_warning(state_row) -> str | None:
    """Surfaced unprompted. The user did not ask, and that is the point."""
    if state_row is None:
        return None
    health = state_row.health or {}
    msgs = []
    for axis, label in (
        ("emotional_safety", "she may not feel safe in this exchange"),
        ("mutual_respect", "the respect in this is one-directional"),
        ("autonomy_preserved", "she has little room to say no here"),
    ):
        a = health.get(axis)
        if a and a.get("value", 1) < 0.55 and a.get("confidence", 0) >= 0.35:
            msgs.append(label)
    p = health.get("pressure_exerted_by_self")
    if p and p.get("value", 0) > 0.35 and p.get("confidence", 0) >= 0.35:
        msgs.append("you are applying more pressure than you probably intend")
    if not msgs:
        return None
    return (
        "Before the suggestions: " + "; ".join(msgs) + ". "
        "This reading is uncertain, but it is the kind of thing worth being "
        "wrong about in the cautious direction."
    )


async def reply(
    session,
    scope: Scope,
    user_id: UUID,
    *,
    track: Track = Track.STANDARD,
) -> ReplyResult:
    """Hot path. No skills loaded, no agent loop."""
    ctx = await ctx_mod.build(session, scope)
    p = make_plan(ctx)
    warning = health_warning(ctx.state_row)

    candidates = await generator.generate(ctx, p, track=track)
    if not candidates:
        return ReplyResult([], p, warning, notes=["generator returned nothing"])

    voice_block = ctx.render_voice()
    avoid = list((ctx.voice or {}).get("avoided_constructions", []))
    humanized = await asyncio.gather(
        *(
            humanizer.humanize(c.text, voice_block, avoid=avoid, track=track)
            for c in candidates
        )
    )

    context_block = ctx.render_conversation(12)
    median_chars = (ctx.voice or {}).get("median_chars")
    verdicts = await asyncio.gather(
        *(
            critic.review(
                text,
                context_block=context_block,
                objective=p.objective.value,
                tactics=p.tactic_keys,
                other_dyad_phrases=ctx.other_dyad_phrases,
                median_chars=median_chars,
            )
            for text in humanized
        )
    )

    scored = [(t, v.scores) for t, v in zip(humanized, verdicts, strict=True)]
    ranked = critic.rank(scored)
    rejected = len(scored) - len(ranked)
    if rejected:
        log.info("candidates_rejected", n=rejected)

    by_text = {c.text: c for c in candidates}
    suggestions: list[Suggestion] = []
    for text, s, score in ranked[:3]:
        # humanize may have rewritten the text; fall back to positional tactic
        src = by_text.get(text)
        idx = humanized.index(text) if text in humanized else 0
        cand = src or candidates[idx]
        suggestions.append(Suggestion(text, cand.tactic, cand.register, score, s))

    if ctx.state_row is not None and suggestions:
        repo = StrategyRepo(session)
        for sug in suggestions[:1]:  # only the recommended one is a prediction
            rec = StrategyRecord(
                relationship_id=scope.relationship_id,
                user_id=user_id,
                objective=p.objective,
                tactic_keys=tuple(p.tactic_keys) or (sug.tactic,),
                avoided_keys=tuple(p.avoided_keys),
                relationship_state_id=ctx.state_row.id,
                preference_model_id=ctx.preference.id if ctx.preference else None,
                rendered_text_sha256=sha256(sug.text),
                rendered_text=sug.text,
                n_candidates=len(candidates),
                critic=sug.scores,
                predicted_positive=p.predicted_positive,
                prediction_basis=p.prediction_basis,
                user_action=UserAction.NOT_YET,
                model_version=MODEL_VERSION,
            )
            sug.strategy_id = await repo.record(scope, rec)

    notes = []
    if p.exploration:
        notes.append(
            "This is an exploratory move -- there is not enough history with "
            "her for the prediction to mean much yet."
        )
    return ReplyResult(suggestions, p, warning, rejected, notes)


_ANALYSIS_SYSTEM = """You analyse a relationship for the person in it.

Rules you do not get to relax:
- Every claim about her state cites what it is based on.
- Where the state block says a reading is uncertain, say it is uncertain.
  Do not launder a 0.4-confidence number into a confident sentence.
- Always give at least one alternative explanation for the main reading.
- Politeness, responsiveness and friendliness are the base rate. They are not
  evidence of attraction on their own, and you must not present them as such.
- If the health block shows anything concerning, lead with it, even though
  that is not what was asked.
- You are talking to an adult about a real person who is not in the room.
  Describe behaviour, not verdicts about her character."""


_INTIMATE_FRAME = """This conversation is between adults about their own sex life
with a partner.

Speak plainly. Use ordinary adult words for ordinary adult things -- euphemism
here is not tact, it is a failure to answer. Do not write clinically, do not
write as though aroused, and do not add disclaimers, warnings or moral framing
the user did not ask for. They came for help with a relationship.

Two rules do not relax here, and they are the reason this track can exist at
all:

- You may not infer anyone's sexual preferences from their personality, work,
  appearance, history or manner. Preferences are known when stated. Otherwise
  they are unknown, and the useful answer is how to find out.
- Consent is specific, current and revocable. Never help get past a no, wear
  someone down, or reframe reluctance as an obstacle."""


async def analyze(
    session,
    scope: Scope,
    question: str,
    intent: Intent,
    *,
    track: Track | None = None,
) -> str:
    """Slow path. Skills loaded, reasoning allowed to take its time."""
    track = track or intent.track
    ctx = await ctx_mod.build(session, scope)
    skill_intent = Intent.INTIMACY.value if track is Track.INTIMATE else intent.value
    picked = skills.select(skill_intent)
    skill_block = skills.render(picked)
    log.info("slow_path", intent=intent.value, skills=[s.name for s in picked])

    frame = _ANALYSIS_SYSTEM
    if track is Track.INTIMATE:
        frame = f"{_ANALYSIS_SYSTEM}\n\n{_INTIMATE_FRAME}"

    out = await router().complete(
        Task.REASON,
        [Msg(role="user", content=f"{ctx.render()}\n\nQUESTION: {question}")],
        track=track,
        system=f"{frame}\n\n{skill_block}",
        max_tokens=2000,
        temperature=0.7,
    )
    warning = health_warning(ctx.state_row)
    return f"{warning}\n\n{out.text}" if warning else out.text


async def handle(session, scope: Scope, user_id: UUID, text: str):
    request = route_request(text)
    if request.intent.is_hot:
        return await reply(session, scope, user_id, track=request.track)
    return await analyze(
        session,
        scope,
        request.text,
        request.intent,
        track=request.track,
    )
