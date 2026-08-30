"""Reply candidate generation (doc s25).

Generates a spread, not a best guess. Three things depend on the spread:
the Critic needs something to rank, the user needs a real choice, and the
learning loop needs variation to learn from -- a system that always sends the
same tactic never finds out what else would have worked.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, Field

from ..llm import Msg, Task, Track, router
from .context import AgentContext
from .strategy import Plan

N_CANDIDATES = 8


class _Candidate(BaseModel):
    text: str = Field(max_length=600)
    tactic: str = Field(max_length=60)
    tone: str = Field(
        description='"lighter" | "warmer" | "more ambiguous" | "more direct"'
    )


class _Batch(BaseModel):
    candidates: list[_Candidate]


_SYSTEM = """You draft candidate replies for the user to send. You are drafting
in their voice, not your own.

Hard rules:
- Write only what the user would plausibly type. Their register, their
  length, their punctuation. Sound like a person on their phone.
- One message per candidate, unless this user habitually sends two.
- Never reference anything not present in the provided context. No invented
  shared history, no invented plans, no invented details about her.
- No explanation of the message's own subtext.
- Do not aim for cleverness. Aim for something that would actually get sent.

Spread the candidates across registers: some lighter, some warmer, some more
ambiguous, some more direct. If a tactic is listed as avoided, do not use it
in any candidate.

Where the context says something is unknown, treat it as unknown. Do not
write a message whose premise is a fact you were not given."""


@dataclass(slots=True)
class Candidate:
    text: str
    tactic: str
    register: str


async def generate(
    ctx: AgentContext,
    plan: Plan,
    n: int = N_CANDIDATES,
    *,
    track: Track = Track.STANDARD,
) -> list[Candidate]:
    prompt = (
        f"{ctx.render()}\n\n"
        f"OBJECTIVE: {plan.objective.value} -- {plan.rationale}\n"
        f"WHAT WOULD COUNT AS PROGRESS: {plan.next_milestone}\n"
        f"TACTICS TO USE: {', '.join(plan.tactic_keys) or 'open'}\n"
        f"TACTICS TO AVOID: {', '.join(plan.avoided_keys) or 'none'}\n\n"
        f"Draft {n} candidate replies."
    )
    batch = await router().structured(
        Task.GENERATE,
        [Msg(role="user", content=prompt)],
        _Batch,
        track=track,
        system=_SYSTEM,
        max_tokens=2500,
        temperature=1.0,
    )
    return [
        Candidate(text=c.text.strip(), tactic=c.tactic, register=c.tone)
        for c in batch.candidates
        if c.text.strip()
    ][:n]
