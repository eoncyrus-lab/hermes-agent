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
from collections.abc import Sequence
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
    """Surfaced unprompted. The user did not ask, and that is the point.

    The strings are Chinese while this file's prose is English: they are read
    by the person being advised, and the product is used in Chinese. There is
    no i18n layer because there is one language -- inventing one for a single
    locale would be more machinery than it replaces.
    """
    if state_row is None:
        return None
    health = state_row.health or {}
    msgs = []
    for axis, label in (
        ("emotional_safety", "她在这段交流里可能并不觉得安全"),
        ("mutual_respect", "这里面的尊重是单向的"),
        ("autonomy_preserved", "她在这里几乎没有说不的余地"),
    ):
        a = health.get(axis)
        if a and a.get("value", 1) < 0.55 and a.get("confidence", 0) >= 0.35:
            msgs.append(label)
    p = health.get("pressure_exerted_by_self")
    if p and p.get("value", 0) > 0.35 and p.get("confidence", 0) >= 0.35:
        msgs.append("你给的压力可能比你以为的大")
    if not msgs:
        return None
    return (
        "先说一句："
        + "；".join(msgs)
        + "。\n这个判断本身不确定，但这类事情宁可谨慎一点。"
    )


async def _bind_user_llm(session, scope: Scope) -> None:
    """Make this user's own model credentials active for what follows.

    Gated on the identity claim rather than on a flag: a queued worker job
    builds a `Scope` with no claim, and the worker must keep running on the
    deployment's own providers. That is not incidental -- extraction is a
    safety task, and a job that picked up a user's endpoint would be exactly
    the route the credential rules exist to prevent.

    A user with no credentials of their own changes nothing, which is the
    common case and costs one query.
    """
    if not scope.identity_claim:
        return
    # Imported from the module, not the package: `sonder.llm` re-exports the
    # `router` *function*, so `from ..llm import router` binds that instead of
    # the module and activation would raise AttributeError the first time a
    # user with credentials made a request.
    from ..llm.router import activate_user
    from ..llm.user_credentials import load_for

    credentials = await load_for(session, scope.user_id)
    if not credentials:
        return
    refused = activate_user(scope.user_id, credentials)
    if refused:
        # Their key is real but unused, and silence would leave them
        # debugging a route that is doing what it was told.
        log.warning("user_provider_not_applied", providers=refused)


async def reply(
    session,
    scope: Scope,
    user_id: UUID,
    *,
    track: Track = Track.STANDARD,
    fresh: Sequence[ctx_mod.FreshTurn] = (),
) -> ReplyResult:
    """Hot path. No skills loaded, no agent loop.

    `fresh` is the text the caller handed over with this request. Until this
    parameter existed the hot path read the conversation exclusively from
    `interactions.last_messages`, so a request carrying the conversation --
    which is every request the browser page and the bot actually send --
    reached the generator as `CONVERSATION: (none)` and was answered with
    generic small talk. The text was parsed for its track and then dropped.
    """
    await _bind_user_llm(session, scope)
    ctx = await ctx_mod.build(session, scope, fresh_turns=fresh)
    p = make_plan(ctx)
    warning = health_warning(ctx.state_row)

    candidates = await generator.generate(ctx, p, track=track)
    if not candidates:
        return ReplyResult([], p, warning, notes=["没有生成出任何候选"])

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
        notes.append("这是一步试探——跟她之间的历史还太少，预测暂时说明不了什么。")
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
    await _bind_user_llm(session, scope)
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
        # This is the slow path, and the one task this deployment routes to a
        # model that reasons. There, `max_tokens` covers the reasoning as well
        # as the answer -- so a budget sized for output alone, which is what
        # 2000 was and what an Anthropic model would have taken it to mean,
        # gets spent before a single character is emitted. It worked on short
        # questions and returned nothing on long ones, which is the worst way
        # for a limit to behave: the caller gets an empty string, not an error.
        max_tokens=8000,
        temperature=0.7,
    )
    warning = health_warning(ctx.state_row)
    return f"{warning}\n\n{out.text}" if warning else out.text


def handed_over(request: RoutedRequest) -> list[ctx_mod.FreshTurn]:
    """The caller's own words, as context for the reply they asked for.

    One block, not parsed into turns: a pasted conversation has no reliable
    speaker structure, and guessing one would put words in someone's mouth
    inside the very prompt that decides what to say to them. The label the
    renderer gives it is deliberately neutral, because this text is sometimes
    the conversation ("她说：在忙吗") and sometimes an instruction ("帮我回一
    句") -- and the generator is better off seeing which than not seeing it at
    all, which is what happened before.
    """
    body = request.text.strip()
    return [ctx_mod.FreshTurn(body=body)] if body else []


async def handle(session, scope: Scope, user_id: UUID, text: str):
    request = route_request(text)
    if request.intent.is_hot:
        return await reply(
            session,
            scope,
            user_id,
            track=request.track,
            fresh=handed_over(request),
        )
    return await analyze(
        session,
        scope,
        request.text,
        request.intent,
        track=request.track,
    )
