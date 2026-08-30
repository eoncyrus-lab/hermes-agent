"""Screenshot / export -> Interaction.

A vision model does the segmentation directly. OCR plus bubble-geometry
heuristics was the obvious alternative and is worse here: speaker attribution
in a chat screenshot depends on layout, colour and reading order together,
which is exactly what a VLM already does and what a rules engine over OCR
boxes gets wrong on every unfamiliar client.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import structlog
from pydantic import BaseModel, Field

from ..domain.schemas.interaction import (
    Channel,
    IngestSource,
    Interaction,
    Message,
    Speaker,
)
from ..llm import Msg, Task, router
from .features import extract, sha256

log = structlog.get_logger(__name__)
EXTRACTOR_VERSION = "ingest/1.0.0"

_SYSTEM = """You transcribe chat screenshots into structured data.

Rules:
- Transcribe verbatim. Never paraphrase, correct, translate or complete.
- `sender` is "self" for the account that owns the screenshot (normally the
  right-hand / outgoing bubbles) and "target" for the other person. Use
  "unknown" when the layout genuinely does not say.
- Copy timestamps exactly as shown. If a message has no visible timestamp,
  leave it null. Do not interpolate.
- Preserve message boundaries: three consecutive bubbles are three messages.
- Do not interpret, summarise, or comment on the content."""


class ParsedMessage(BaseModel):
    sender: str = Field(description='"self" | "target" | "unknown"')
    text: str
    timestamp_text: str | None = Field(
        default=None, description="Verbatim, as displayed. Null when absent."
    )


class ParsedConversation(BaseModel):
    messages: list[ParsedMessage]
    channel_guess: str | None = None
    date_hint: str | None = Field(
        default=None, description="Any date header visible in the screenshot."
    )
    unreadable_regions: int = Field(
        default=0, description="Count of bubbles that could not be transcribed."
    )


async def parse_screenshots(images: list[bytes]) -> ParsedConversation:
    r = router()
    msg = Msg(
        role="user",
        content=(
            "Transcribe these chat screenshots in order. They are consecutive "
            "scroll positions of one conversation; drop bubbles that are "
            "duplicated across the overlap."
        ),
        images=images,
    )
    return await r.structured(
        Task.VISION, [msg], ParsedConversation, system=_SYSTEM, max_tokens=8000
    )


def _resolve_time(raw: str | None, date_hint: str | None, fallback: datetime) -> datetime:
    """Best-effort timestamp resolution. Unknown stays close to fallback.

    Deliberately conservative: a wrong timestamp corrupts every latency
    baseline downstream, so an unparseable value inherits the previous
    message's time rather than being guessed at.
    """
    if not raw:
        return fallback
    import re

    m = re.search(r"(\d{1,2}):(\d{2})", raw)
    if not m:
        return fallback
    hh, mm = int(m.group(1)), int(m.group(2))
    if "pm" in raw.lower() or "下午" in raw or "晚上" in raw:
        hh = hh % 12 + 12
    base = fallback
    try:
        return base.replace(hour=hh, minute=mm, second=0, microsecond=0)
    except ValueError:
        return fallback


def to_interaction(
    parsed: ParsedConversation,
    relationship_id: UUID,
    channel: Channel,
    source: IngestSource = IngestSource.SCREENSHOT_OCR,
    source_artifact_id: UUID | None = None,
    anchor: datetime | None = None,
) -> Interaction:
    anchor = anchor or datetime.now(UTC)
    interaction_id = uuid4()
    messages: list[Message] = []
    prior_texts: list[str] = []
    cursor = anchor
    last_by_sender: dict[str, datetime] = {}

    for i, pm in enumerate(parsed.messages):
        try:
            sender = Speaker(pm.sender)
        except ValueError:
            sender = Speaker.UNKNOWN
        ts = _resolve_time(pm.timestamp_text, parsed.date_hint, cursor)
        if ts < cursor:
            ts = cursor
        cursor = ts

        latency = None
        other = Speaker.TARGET if sender is Speaker.SELF else Speaker.SELF
        if other.value in last_by_sender:
            delta = (ts - last_by_sender[other.value]).total_seconds()
            latency = delta if delta >= 0 else None
        last_by_sender[sender.value] = ts

        messages.append(
            Message(
                interaction_id=interaction_id,
                sender=sender,
                sent_at=ts,
                seq=i,
                text=pm.text,
                text_sha256=sha256(pm.text),
                reply_latency_s=latency,
                features=extract(pm.text, prior_texts),
            )
        )
        prior_texts.append(pm.text)

    n_self = sum(1 for m in messages if m.sender is Speaker.SELF)
    n_target = sum(1 for m in messages if m.sender is Speaker.TARGET)
    lat_self = [
        m.reply_latency_s
        for m in messages
        if m.sender is Speaker.SELF and m.reply_latency_s
    ]
    lat_target = [
        m.reply_latency_s
        for m in messages
        if m.sender is Speaker.TARGET and m.reply_latency_s
    ]

    import statistics

    if parsed.unreadable_regions:
        log.warning("ingest_partial", unreadable=parsed.unreadable_regions)

    return Interaction(
        id=interaction_id,
        relationship_id=relationship_id,
        channel=channel,
        source=source,
        started_at=messages[0].sent_at if messages else anchor,
        ended_at=messages[-1].sent_at if messages else anchor,
        messages=tuple(messages),
        message_count_self=n_self,
        message_count_target=n_target,
        initiated_by=messages[0].sender if messages else Speaker.UNKNOWN,
        ended_by=messages[-1].sender if messages else Speaker.UNKNOWN,
        median_latency_self_s=statistics.median(lat_self) if lat_self else None,
        median_latency_target_s=statistics.median(lat_target) if lat_target else None,
        source_artifact_id=source_artifact_id,
        extractor_version=EXTRACTOR_VERSION,
    )
