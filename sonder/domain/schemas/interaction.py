"""Schema 3/5 -- Interaction: the observed layer.

Everything else in the system is derived. This is the only place raw fact
lives, so it is strictly append-only. Re-running an extractor produces a new
row with a new `extractor_version`; it never mutates an existing one. That is
what makes "the state changed because the model changed" distinguishable from
"the state changed because she changed".
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from uuid import UUID, uuid4

from pydantic import Field, model_validator

from .primitives import (
    EvidenceRef,
    Frozen,
    Provenance,
    Sensitivity,
    utcnow,
)


class Channel(str, Enum):
    WECHAT = "wechat"
    IMESSAGE = "imessage"
    WHATSAPP = "whatsapp"
    TELEGRAM = "telegram"
    INSTAGRAM = "instagram"
    DATING_APP = "dating_app"
    IN_PERSON = "in_person"
    VOICE_CALL = "voice_call"
    OTHER = "other"


class IngestSource(str, Enum):
    SCREENSHOT_OCR = "screenshot_ocr"
    CHAT_EXPORT = "chat_export"
    MANUAL_ENTRY = "manual_entry"
    VOICE_TRANSCRIPT = "voice_transcript"
    USER_RECOUNT = "user_recount"  # user described it after the fact; lossy


class Speaker(str, Enum):
    SELF = "self"
    TARGET = "target"
    OTHER = "other"
    UNKNOWN = "unknown"


class MessageFeatures(Frozen):
    """Cheap, deterministic-ish features. No interpretation lives here.

    Interpretation belongs in Signal, which carries confidence. Keeping the
    two apart is what lets us recompute beliefs without re-reading raw text.
    """

    char_len: int = Field(ge=0)
    is_question: bool = False
    ends_open: bool = False  # invites continuation
    self_disclosure: bool = False
    references_prior: bool = False  # callback / inside joke
    contains_emoji: bool = False
    emoji_count: int = Field(default=0, ge=0)
    contains_invitation: bool = False
    contains_plan_commitment: bool = False
    topic_shift: bool = False
    extractor_version: str = Field(max_length=64)


class Message(Frozen):
    id: UUID = Field(default_factory=uuid4)
    interaction_id: UUID
    sender: Speaker
    sent_at: datetime
    seq: int = Field(ge=0)
    text: str | None = Field(default=None, repr=False)
    text_sha256: str = Field(max_length=64)
    reply_latency_s: float | None = Field(default=None, ge=0)
    features: MessageFeatures | None = None
    sensitivity: Sensitivity = Sensitivity.STANDARD
    redacted: bool = False

    @model_validator(mode="after")
    def _vault_text_not_inline(self) -> Message:
        if self.sensitivity is Sensitivity.VAULT and self.text is not None:
            raise ValueError("vault-tier message text must live in the vault store")
        return self


class SignalType(str, Enum):
    """Neutral, observation-level vocabulary (doc s4 rename).

    These are descriptions of behaviour, not verdicts about a person.
    """

    ENGAGEMENT_UP = "engagement_up"
    ENGAGEMENT_DOWN = "engagement_down"
    INITIATIVE = "initiative"
    RECIPROCATION = "reciprocation"
    SELF_DISCLOSURE = "self_disclosure"
    PLAYFUL_CHALLENGE = "playful_challenge"
    TEASING_RETURNED = "teasing_returned"
    WARMTH = "warmth"
    AVOIDANCE = "avoidance"
    BOUNDARY_SET = "boundary_set"
    PLAN_ACCEPTED = "plan_accepted"
    PLAN_DEFLECTED = "plan_deflected"
    CONFLICT = "conflict"
    REPAIR_ATTEMPT = "repair_attempt"
    FLIRT_INITIATED = "flirt_initiated"
    FLIRT_RECIPROCATED = "flirt_reciprocated"
    DISCOMFORT = "discomfort"


class Signal(Frozen):
    """doc s47. One auditable row per interpreted observation."""

    id: UUID = Field(default_factory=uuid4)
    relationship_id: UUID
    interaction_id: UUID
    type: SignalType
    occurred_at: datetime
    magnitude: float = Field(ge=-1.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    by: Speaker
    evidence_message_ids: tuple[UUID, ...] = Field(min_length=1)
    baseline_note: str | None = Field(
        default=None,
        max_length=240,
        description="What this is relative to. A 4-minute reply is only a signal "
        "against her own median, never an absolute.",
    )
    extractor_version: str = Field(max_length=64)
    created_at: datetime = Field(default_factory=utcnow)

    def as_evidence(self) -> EvidenceRef:
        from .primitives import EvidenceKind

        return EvidenceRef(
            kind=EvidenceKind.SIGNAL, ref_id=self.id, occurred_at=self.occurred_at
        )


class Interaction(Frozen):
    """One bounded exchange: a chat session, a date, a call.

    The unit the Trend Engine aggregates over (doc s21) and the unit a
    StrategyOutcome is measured against.
    """

    id: UUID = Field(default_factory=uuid4)
    relationship_id: UUID
    channel: Channel
    source: IngestSource
    started_at: datetime
    ended_at: datetime | None = None

    messages: tuple[Message, ...] = ()
    signals: tuple[Signal, ...] = ()

    # session-level observations, all directly countable
    message_count_self: int = Field(default=0, ge=0)
    message_count_target: int = Field(default=0, ge=0)
    initiated_by: Speaker = Speaker.UNKNOWN
    ended_by: Speaker = Speaker.UNKNOWN
    median_latency_self_s: float | None = Field(default=None, ge=0)
    median_latency_target_s: float | None = Field(default=None, ge=0)

    source_artifact_id: UUID | None = Field(
        default=None,
        description="Screenshot/export blob. Deletable independently of the "
        "extracted interaction (doc s51).",
    )
    source_artifact_deleted: bool = False
    provenance: Provenance = Provenance.OBSERVED_BEHAVIOR
    sensitivity: Sensitivity = Sensitivity.STANDARD
    extractor_version: str = Field(max_length=64)
    ingested_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _coherent(self) -> Interaction:
        if self.ended_at and self.ended_at < self.started_at:
            raise ValueError("ended_at precedes started_at")
        if self.source is IngestSource.USER_RECOUNT and self.messages:
            raise ValueError(
                "USER_RECOUNT carries no verbatim messages; record signals only"
            )
        return self

    @property
    def initiative_ratio(self) -> float | None:
        total = self.message_count_self + self.message_count_target
        return None if total == 0 else self.message_count_target / total
