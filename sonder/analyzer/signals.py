"""Messages -> Signals. The one place interpretation happens on the observed layer.

Two disciplines are enforced here rather than requested in the prompt:

  * every signal cites the message ids it came from, so `record_signal` is
    auditable and "why does it think her interest rose" is answerable
  * every signal is stated relative to that person's own baseline; where no
    baseline exists yet, latency and length signals are suppressed entirely
    rather than emitted against an imagined norm
"""

from __future__ import annotations

from uuid import NAMESPACE_URL, UUID, uuid5

import structlog
from pydantic import BaseModel, Field

from ..domain.schemas.interaction import Signal, SignalType, Speaker
from ..llm import Msg, Task, router
from .features import Baseline

log = structlog.get_logger(__name__)
EXTRACTOR_VERSION = "signals/1.0.0"

_SYSTEM = """You label observable behaviour in a conversation. You are not
assessing whether anyone likes anyone.

For each signal you emit:
- cite the 1-based message indices it comes from
- give magnitude in [-1, 1] and confidence in [0, 1]
- confidence above 0.7 requires the behaviour to be unambiguous in the text

Do not emit a signal for ordinary politeness, acknowledgement, or a
conversation simply continuing. Those are the baseline, not evidence. An
empty list is the correct output for a routine exchange.

Available types:
engagement_up, engagement_down, initiative, reciprocation, self_disclosure,
playful_challenge, teasing_returned, warmth, avoidance, boundary_set,
plan_accepted, plan_deflected, conflict, repair_attempt, flirt_initiated,
flirt_reciprocated, discomfort"""


class RawSignal(BaseModel):
    type: str
    by: str = Field(description='"self" | "target"')
    message_indices: list[int] = Field(min_length=1)
    magnitude: float
    confidence: float
    rationale: str = Field(max_length=200)


class SignalBatch(BaseModel):
    signals: list[RawSignal]


def evidence_identity(
    relationship_id: UUID,
    interaction_id: UUID,
    signal_type: SignalType,
    by: Speaker,
    message_ids: tuple[UUID, ...],
) -> UUID:
    """Stable identity for one interpretation of the same source evidence."""
    source = ":".join(
        (
            str(relationship_id),
            str(interaction_id),
            signal_type.value,
            by.value,
            *(str(item) for item in sorted(set(message_ids), key=str)),
        )
    )
    return uuid5(NAMESPACE_URL, f"sonder:signal:{source}")


def _render(messages: list) -> str:
    lines = []
    for i, m in enumerate(messages, start=1):
        who = m.sender.value if hasattr(m.sender, "value") else m.sender
        body = m.text if getattr(m, "text", None) else "[redacted]"
        lat = f" (+{int(m.reply_latency_s)}s)" if m.reply_latency_s else ""
        lines.append(f"[{i}] {who}{lat}: {body}")
    return "\n".join(lines)


async def extract_signals(
    messages: list,
    relationship_id: UUID,
    interaction_id: UUID,
    target_baseline: Baseline | None = None,
) -> list[Signal]:
    if not messages:
        return []

    context = ""
    if target_baseline and target_baseline.established:
        context = (
            f"\n\nHer own norms in this relationship: median reply "
            f"{int(target_baseline.median_latency or 0)}s, median message "
            f"{int(target_baseline.median_length or 0)} chars. Judge deviation "
            f"against these, never against a general expectation."
        )
    else:
        context = (
            "\n\nThere is no established baseline for this person yet. Do NOT "
            "emit engagement_up/engagement_down based on reply speed or "
            "message length; you have nothing to compare against."
        )

    batch = await router().structured(
        Task.EXTRACT,
        [Msg(role="user", content=_render(messages) + context)],
        SignalBatch,
        system=_SYSTEM,
        max_tokens=3000,
    )

    out: list[Signal] = []
    seen: set[UUID] = set()
    baseline_note = (
        f"vs own median {int(target_baseline.median_latency or 0)}s"
        if target_baseline and target_baseline.established
        else "no baseline established"
    )
    for raw in batch.signals:
        try:
            sig_type = SignalType(raw.type)
            by = Speaker(raw.by)
        except ValueError:
            log.warning("unknown_signal_type", type=raw.type, by=raw.by)
            continue
        idx = [i for i in raw.message_indices if 1 <= i <= len(messages)]
        if not idx:
            continue
        ids = tuple(messages[i - 1].id for i in idx)
        stable_id = evidence_identity(relationship_id, interaction_id, sig_type, by, ids)
        if stable_id in seen:
            continue
        seen.add(stable_id)
        anchor = messages[idx[-1] - 1]
        out.append(
            Signal(
                id=stable_id,
                relationship_id=relationship_id,
                interaction_id=interaction_id,
                type=sig_type,
                occurred_at=anchor.sent_at,
                magnitude=max(-1.0, min(1.0, raw.magnitude)),
                confidence=max(0.0, min(1.0, raw.confidence)),
                by=by,
                evidence_message_ids=ids,
                baseline_note=f"{baseline_note}; {raw.rationale}"[:240],
                extractor_version=EXTRACTOR_VERSION,
            )
        )
    return out
