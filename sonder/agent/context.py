"""Context assembly. The single path from storage into a prompt.

Everything the model ever sees about people comes through here, and it takes
exactly one Scope. That makes cross-relationship leakage a question about one
function instead of a question about every call site.

It also decides what NOT to include. The temptation is to pass the whole
history and let the model sort it out; that is how a system ends up
re-deriving beliefs from raw text on every turn, inconsistently.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from ..domain.schemas.preference import PreferenceModel
from ..domain.schemas.primitives import BeliefStatus
from ..state.trend import Trend, summarise
from ..storage.repositories import (
    InteractionRepo,
    MemoryRepo,
    PreferenceRepo,
    StateRepo,
)
from ..storage.scope import Scope


@dataclass(frozen=True, slots=True)
class FreshTurn:
    """A turn the caller handed over with the request, not read from the record.

    Deliberately not an `Interaction`, and deliberately not written here.
    Invariant 1 says a model-inferred assessment needs an evidence ref, and
    this text has none yet. It reaches the generator and the Critic so they can
    answer the thing actually in front of the user -- the hot path used to read
    only `interactions.last_messages`, so a request that carried the
    conversation was invisible to both and the reply was drafted against
    `CONVERSATION: (none)`.

    It becomes evidence the same way every other observation does: by going
    through ingest. Passing text here must never be a way to skip that.
    """

    body: str
    # "unknown" is the honest label for a block a user pasted, which has no
    # reliable speaker structure. The renderer omits it rather than printing
    # a speaker the caller never claimed.
    speaker: str = "unknown"


@dataclass(slots=True)
class AgentContext:
    scope: Scope
    recent_messages: list = field(default_factory=list)
    fresh_turns: list[FreshTurn] = field(default_factory=list)
    state_row: object | None = None
    preference: PreferenceModel | None = None
    trends: list[Trend] = field(default_factory=list)
    memories: list = field(default_factory=list)
    voice: dict | None = None
    # None means the production cross-dyad reader is disabled. An empty set
    # would mean a reader actually ran and found no phrases; keep those states
    # distinct so a downstream risk=0 is never described as "checked".
    other_dyad_phrases: set[str] | None = None

    # ---------------------------------------------------------------- render

    def render_state(self) -> str:
        """Only what the agent is entitled to act on, with its uncertainty."""
        if self.state_row is None:
            return (
                "RELATIONSHIP STATE: none computed yet. Treat every reading as "
                "unknown and say so rather than inferring one from the "
                "conversation in front of you."
            )
        lines = [f"RELATIONSHIP STATE (as of {self.state_row.as_of:%Y-%m-%d}):"]
        for group in ("attraction", "intimacy", "bond", "dynamics", "health"):
            payload = getattr(self.state_row, group, None) or {}
            for axis, a in payload.items():
                if not isinstance(a, dict):
                    continue
                conf = a.get("confidence", 0)
                val = a.get("value")
                if conf >= 0.6:
                    lines.append(f"  {group}.{axis}: {val:.2f} (confidence {conf:.2f})")
                else:
                    lines.append(
                        f"  {group}.{axis}: UNCERTAIN ({val:.2f}, confidence "
                        f"{conf:.2f}) -- do not rely on this"
                    )
        lines.append(
            f"  stage (label only, not a decision input): {self.state_row.stage}"
        )
        return "\n".join(lines)

    def render_preferences(self) -> str:
        if not self.preference or not self.preference.entries:
            return (
                "PREFERENCES: nothing established. Do not guess at what she "
                "responds to; vary deliberately and observe."
            )
        lines = ["WHAT SHE HAS RESPONDED TO (this person only):"]
        ordered = sorted(
            self.preference.entries.values(),
            key=lambda e: e.estimate.mean,
            reverse=True,
        )
        for entry in ordered:
            if entry.status is BeliefStatus.UNKNOWN:
                continue
            lines.append(f"  {entry.statement()}")
        unknown = [
            k
            for k, e in self.preference.entries.items()
            if e.status is BeliefStatus.UNKNOWN
        ]
        if unknown:
            lines.append(f"  still unknown: {', '.join(sorted(unknown)[:8])}")
        return "\n".join(lines) if len(lines) > 1 else "PREFERENCES: nothing established."

    def render_trends(self) -> str:
        if not self.trends:
            return "TRENDS: not enough history to state a direction."
        return "TRENDS:\n" + "\n".join(f"  {t.describe()}" for t in self.trends)

    def render_voice(self) -> str:
        if not self.voice or self.voice.get("n_samples", 0) < 40:
            return (
                "VOICE: not enough samples to model this user's register. Write "
                "plainly and short; do not invent a personality for them."
            )
        v = self.voice
        bits = [
            f"median message {v.get('median_chars')} chars",
            f"emoji in {v.get('emoji_rate', 0):.0%} of messages",
            f"questions in {v.get('question_rate', 0):.0%}",
        ]
        out = ["USER'S OWN REGISTER (match this):", "  " + "; ".join(bits)]
        if v.get("recurring_phrases"):
            out.append(f"  recurring: {', '.join(v['recurring_phrases'][:8])}")
        if v.get("avoided_constructions"):
            out.append(f"  never writes: {', '.join(v['avoided_constructions'][:8])}")
        return "\n".join(out)

    def render_conversation(self, n: int = 25) -> str:
        lines: list[str] = []
        if self.recent_messages:
            lines.append("RECENT CONVERSATION:")
            for m in self.recent_messages[-n:]:
                body = m.body if getattr(m, "body", None) else "[redacted]"
                lines.append(f"  {m.sender}: {body}")
        if self.fresh_turns:
            if lines:
                lines.append("")
            # Kept under its own heading, and after the stored history, for two
            # reasons. The model must know this is the newest thing said, and
            # it must not read as though the record already contains it -- the
            # Critic scores context_match against this block, and an operator
            # reading a transcript later should be able to tell which turns
            # were persisted and which were merely handed over.
            lines.append("HANDED OVER WITH THIS REQUEST (newest; not yet in the record):")
            for t in self.fresh_turns:
                prefix = "" if t.speaker in ("", "unknown") else f"{t.speaker}: "
                lines.append(f"  {prefix}{t.body}")
        if not lines:
            return "CONVERSATION: (none)"
        return "\n".join(lines)

    def render_memory(self) -> str:
        if not self.memories:
            return ""
        return "RELEVANT HISTORY (this relationship only):\n" + "\n".join(
            f"  - {m.summary}" for m in self.memories
        )

    def render(self) -> str:
        blocks = [
            self.render_state(),
            self.render_preferences(),
            self.render_trends(),
            self.render_memory(),
            self.render_voice(),
            self.render_conversation(),
        ]
        return "\n\n".join(b for b in blocks if b)


async def build(
    session,
    scope: Scope,
    *,
    n_messages: int = 30,
    query_embedding: list[float] | None = None,
    include_leak_guard: bool = True,
    fresh_turns: Sequence[FreshTurn] = (),
) -> AgentContext:
    """Everything the agent is allowed to reason from, for one relationship.

    `fresh_turns` is what the caller handed over with this request. It is
    rendered into the conversation block so the hot path can answer the
    message actually in front of the user, and it is *not* persisted: this
    function reads, it does not write. Durable evidence still arrives only
    through the ingest pipeline, which is the one place that assigns an
    interaction id and an evidence ref.
    """
    interactions = InteractionRepo(session)
    states = StateRepo(session)
    prefs = PreferenceRepo(session)
    memory = MemoryRepo(session)

    ctx = AgentContext(scope=scope)
    ctx.fresh_turns = list(fresh_turns)
    ctx.recent_messages = await interactions.last_messages(scope, n_messages)
    ctx.state_row = await states.current(scope)

    pm_row = await prefs.current_model(scope)
    if pm_row is not None:
        ctx.preference = PreferenceRepo.hydrate(pm_row)

    history = await states.history(scope, limit=60)
    ctx.trends = summarise(history)

    if query_embedding is not None:
        ctx.memories = await memory.search(scope, query_embedding, k=6)

    # Cross-dyad text access is intentionally disabled. A shared DB role plus
    # caller-set session scope cannot authenticate account ownership strongly
    # enough to expose another relationship's text, even to a leak detector.
    # The critic therefore degrades to its remaining boundary checks.
    # Preserve the argument for call-site compatibility. Production cannot
    # safely populate this without a separately authenticated, user-scoped
    # reader, so both values leave the dormant helper unavailable.
    if include_leak_guard:
        ctx.other_dyad_phrases = None

    return ctx


def stale(ctx: AgentContext, max_age_days: int = 7) -> bool:
    """Whether the state is old enough that it should be recomputed first."""
    if ctx.state_row is None:
        return True
    age = datetime.now(UTC) - ctx.state_row.as_of
    return age > timedelta(days=max_age_days)
