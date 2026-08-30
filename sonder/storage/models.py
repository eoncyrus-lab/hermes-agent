"""SQLAlchemy mapping. Mirrors migrations/001_core.sql.

The pydantic schemas in sonder/domain/schemas are the source of truth for
shape and invariants; these are the storage projection. Nested value objects
(Assessment, axes, critic scores) are stored as JSONB rather than shredded
into columns: they are read and written whole, and shredding them would put
the invariants in two places.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ENUM, JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


# The migration creates these as real Postgres enum types, so the mapping must
# use them too: asyncpg will not implicitly cast varchar into an enum column,
# and declaring them as String here fails only at insert time against a real
# database. create_type=False because migrations own the DDL.
def _enum(name: str, *values: str) -> ENUM:
    return ENUM(*values, name=name, create_type=False)


PROVENANCE = _enum(
    "provenance",
    "user_stated",
    "target_stated",
    "observed",
    "model_inference",
    "imported",
)
SENSITIVITY = _enum("sensitivity", "standard", "sensitive", "vault")
DATA_SUBJECT = _enum("data_subject", "self", "third_party")
SPEAKER = _enum("speaker", "self", "target", "other", "unknown")
DIRECTION = _enum("direction", "positive", "neutral", "negative")
CONSENT_STATUS = _enum("consent_status", "confirmed", "declined", "hard_limit", "unknown")
OUTCOME_LABEL = _enum("outcome_label", "positive", "neutral", "negative", "indeterminate")


def _pk() -> Mapped[UUID]:
    return mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)


def _ts(**kw) -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), **kw)


class User(Base):
    __tablename__ = "users"
    id: Mapped[UUID] = _pk()
    created_at: Mapped[datetime] = _ts(default=lambda: datetime.now(UTC))


class PersonProfileRow(Base):
    __tablename__ = "person_profiles"
    id: Mapped[UUID] = _pk()
    user_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    subject: Mapped[str] = mapped_column(String(16))
    data_subject: Mapped[str] = mapped_column(DATA_SUBJECT)
    display_name: Mapped[str] = mapped_column(String(80))
    is_pseudonym: Mapped[bool] = mapped_column(Boolean, default=True)
    version: Mapped[int] = mapped_column(Integer)
    supersedes: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    attributes: Mapped[dict] = mapped_column(JSONB, default=dict)
    traits: Mapped[dict] = mapped_column(JSONB, default=dict)
    preference_model_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True), nullable=True
    )
    voice_profile_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True), nullable=True
    )
    first_seen_at: Mapped[datetime | None] = _ts(nullable=True)
    last_interaction_at: Mapped[datetime | None] = _ts(nullable=True)
    sensitivity: Mapped[str] = mapped_column(SENSITIVITY, default="standard")
    retention_until: Mapped[datetime | None] = _ts(nullable=True)
    deletion_requested_at: Mapped[datetime | None] = _ts(nullable=True)
    created_at: Mapped[datetime] = _ts(default=lambda: datetime.now(UTC))

    __table_args__ = (
        CheckConstraint(
            "subject <> 'target' OR data_subject = 'third_party'",
            name="target_is_third_party",
        ),
        CheckConstraint(
            "data_subject <> 'third_party' OR retention_until IS NOT NULL",
            name="third_party_expires",
        ),
        UniqueConstraint("id", "user_id"),
        ForeignKeyConstraint(
            ["supersedes", "user_id"],
            ["person_profiles.id", "person_profiles.user_id"],
            name="person_profiles_supersedes_same_user",
        ),
        ForeignKeyConstraint(
            ["preference_model_id", "id"],
            ["preference_models.id", "preference_models.person_id"],
            name="person_profiles_preference_model_same_person",
            use_alter=True,
        ),
        ForeignKeyConstraint(
            ["voice_profile_id", "id"],
            ["voice_profiles.id", "voice_profiles.person_id"],
            name="person_profiles_voice_profile_same_person",
            use_alter=True,
        ),
    )


class VoiceProfileRow(Base):
    """User-scoped on purpose: your register does not change per person."""

    __tablename__ = "voice_profiles"
    id: Mapped[UUID] = _pk()
    person_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("person_profiles.id", ondelete="CASCADE")
    )
    version: Mapped[int] = mapped_column(Integer, default=1)
    n_samples: Mapped[int] = mapped_column(Integer, default=0)
    features: Mapped[dict] = mapped_column(JSONB, default=dict)
    recurring_phrases: Mapped[list] = mapped_column(ARRAY(Text), default=list)
    avoided_constructions: Mapped[list] = mapped_column(ARRAY(Text), default=list)
    exemplar_message_ids: Mapped[list] = mapped_column(
        ARRAY(PgUUID(as_uuid=True)), default=list
    )
    created_at: Mapped[datetime] = _ts(default=lambda: datetime.now(UTC))

    __table_args__ = (
        UniqueConstraint("person_id", "version"),
        UniqueConstraint("id", "person_id"),
    )


class RelationshipRow(Base):
    __tablename__ = "relationships"
    id: Mapped[UUID] = _pk()
    user_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    target_person_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("person_profiles.id", ondelete="CASCADE")
    )
    started_at: Mapped[datetime] = _ts(default=lambda: datetime.now(UTC))
    archived_at: Mapped[datetime | None] = _ts(nullable=True)


class InteractionRow(Base):
    __tablename__ = "interactions"
    id: Mapped[UUID] = _pk()
    relationship_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("relationships.id", ondelete="CASCADE"),
        index=True,
    )
    channel: Mapped[str] = mapped_column(String(32))
    source: Mapped[str] = mapped_column(String(32))
    started_at: Mapped[datetime] = _ts()
    ended_at: Mapped[datetime | None] = _ts(nullable=True)
    message_count_self: Mapped[int] = mapped_column(Integer, default=0)
    message_count_target: Mapped[int] = mapped_column(Integer, default=0)
    initiated_by: Mapped[str] = mapped_column(SPEAKER, default="unknown")
    ended_by: Mapped[str] = mapped_column(SPEAKER, default="unknown")
    median_latency_self_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    median_latency_target_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    source_artifact_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True), nullable=True
    )
    source_artifact_deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    sensitivity: Mapped[str] = mapped_column(SENSITIVITY, default="standard")
    extractor_version: Mapped[str] = mapped_column(String(64))
    ingested_at: Mapped[datetime] = _ts(default=lambda: datetime.now(UTC))


class MessageRow(Base):
    __tablename__ = "messages"
    id: Mapped[UUID] = _pk()
    interaction_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("interactions.id", ondelete="CASCADE"),
        index=True,
    )
    sender: Mapped[str] = mapped_column(SPEAKER)
    sent_at: Mapped[datetime] = _ts()
    seq: Mapped[int] = mapped_column(Integer)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    text_sha256: Mapped[str] = mapped_column(String(64))
    reply_latency_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    features: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    sensitivity: Mapped[str] = mapped_column(SENSITIVITY, default="standard")
    redacted: Mapped[bool] = mapped_column(Boolean, default=False)

    __table_args__ = (
        UniqueConstraint("interaction_id", "seq"),
        CheckConstraint(
            "sensitivity <> 'vault' OR body IS NULL", name="vault_text_not_inline"
        ),
    )


class SignalRow(Base):
    __tablename__ = "signals"
    id: Mapped[UUID] = _pk()
    relationship_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("relationships.id", ondelete="CASCADE"),
        index=True,
    )
    interaction_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("interactions.id", ondelete="CASCADE")
    )
    type: Mapped[str] = mapped_column(String(40))
    occurred_at: Mapped[datetime] = _ts()
    magnitude: Mapped[float] = mapped_column(Float)
    confidence: Mapped[float] = mapped_column(Float)
    by_speaker: Mapped[str] = mapped_column(SPEAKER)
    evidence_message_ids: Mapped[list] = mapped_column(ARRAY(PgUUID(as_uuid=True)))
    baseline_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    extractor_version: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = _ts(default=lambda: datetime.now(UTC))


class RelationshipStateRow(Base):
    __tablename__ = "relationship_states"
    id: Mapped[UUID] = _pk()
    relationship_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("relationships.id", ondelete="CASCADE"),
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer)
    supersedes: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    as_of: Mapped[datetime] = _ts()
    window_start: Mapped[datetime | None] = _ts(nullable=True)
    window_end: Mapped[datetime | None] = _ts(nullable=True)
    attraction: Mapped[dict] = mapped_column(JSONB, default=dict)
    intimacy: Mapped[dict] = mapped_column(JSONB, default=dict)
    bond: Mapped[dict] = mapped_column(JSONB, default=dict)
    dynamics: Mapped[dict] = mapped_column(JSONB, default=dict)
    health: Mapped[dict] = mapped_column(JSONB, default=dict)
    compatibility: Mapped[dict] = mapped_column(JSONB, default=dict)
    stage: Mapped[str] = mapped_column(String(32))
    computed_from: Mapped[list] = mapped_column(ARRAY(PgUUID(as_uuid=True)), default=list)
    model_version: Mapped[str] = mapped_column(String(64))
    input_fingerprint: Mapped[str] = mapped_column(String(64))
    computed_at: Mapped[datetime] = _ts(default=lambda: datetime.now(UTC))

    __table_args__ = (
        UniqueConstraint("relationship_id", "version"),
        UniqueConstraint("relationship_id", "input_fingerprint"),
        UniqueConstraint("id", "relationship_id"),
        ForeignKeyConstraint(
            ["supersedes", "relationship_id"],
            ["relationship_states.id", "relationship_states.relationship_id"],
            name="relationship_states_supersedes_same_relationship",
        ),
    )


class PreferenceEvidenceRow(Base):
    __tablename__ = "preference_evidence"
    id: Mapped[UUID] = _pk()
    person_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("person_profiles.id", ondelete="CASCADE")
    )
    relationship_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("relationships.id", ondelete="CASCADE"),
        index=True,
    )
    domain: Mapped[str] = mapped_column(String(32))
    key: Mapped[str] = mapped_column(String(80))
    direction: Mapped[str] = mapped_column(DIRECTION)
    strength: Mapped[float] = mapped_column(Float, default=0.5)
    provenance: Mapped[str] = mapped_column(PROVENANCE)
    observed_at: Mapped[datetime] = _ts()
    source_interaction_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True), nullable=True
    )
    source_signal_ids: Mapped[list] = mapped_column(
        ARRAY(PgUUID(as_uuid=True)), default=list
    )
    source_strategy_outcome_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True), nullable=True
    )
    stage_at_time: Mapped[str | None] = mapped_column(String(40), nullable=True)
    channel: Mapped[str | None] = mapped_column(String(40), nullable=True)
    in_group_setting: Mapped[bool] = mapped_column(Boolean, default=False)
    known_confounders: Mapped[list] = mapped_column(ARRAY(Text), default=list)
    excluded_from_learning: Mapped[bool] = mapped_column(Boolean, default=False)
    sensitivity: Mapped[str] = mapped_column(SENSITIVITY, default="standard")
    recorded_at: Mapped[datetime] = _ts(default=lambda: datetime.now(UTC))

    __table_args__ = (
        CheckConstraint(
            "domain NOT IN ('physical','sexual','kink') OR "
            "(provenance IN ('user_stated','target_stated') AND sensitivity = 'vault')",
            name="protected_domains_need_statement",
        ),
    )


class PreferenceModelRow(Base):
    __tablename__ = "preference_models"
    id: Mapped[UUID] = _pk()
    person_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("person_profiles.id", ondelete="CASCADE")
    )
    relationship_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("relationships.id", ondelete="CASCADE"),
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer)
    entries: Mapped[dict] = mapped_column(JSONB, default=dict)
    scorer_version: Mapped[str] = mapped_column(String(64))
    input_fingerprint: Mapped[str] = mapped_column(String(64))
    computed_at: Mapped[datetime] = _ts(default=lambda: datetime.now(UTC))

    __table_args__ = (
        UniqueConstraint("person_id", "version"),
        UniqueConstraint("relationship_id", "input_fingerprint"),
        UniqueConstraint("id", "relationship_id"),
        UniqueConstraint("id", "person_id"),
    )


class UserTacticPriorRow(Base):
    """User-scoped. The only sanctioned cross-relationship channel."""

    __tablename__ = "user_tactic_priors"
    id: Mapped[UUID] = _pk()
    user_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    tactic_key: Mapped[str] = mapped_column(String(80))
    domain: Mapped[str] = mapped_column(String(32))
    prior_alpha: Mapped[float] = mapped_column(Float)
    prior_beta: Mapped[float] = mapped_column(Float)
    n_relationships: Mapped[int] = mapped_column(Integer)
    computed_at: Mapped[datetime] = _ts(default=lambda: datetime.now(UTC))

    __table_args__ = (
        UniqueConstraint("user_id", "tactic_key"),
        CheckConstraint("n_relationships >= 3", name="min_k"),
        CheckConstraint(
            "domain NOT IN ('physical','sexual','kink')", name="no_protected_priors"
        ),
    )


class StrategyRecordRow(Base):
    __tablename__ = "strategy_records"
    id: Mapped[UUID] = _pk()
    relationship_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("relationships.id", ondelete="CASCADE"),
        index=True,
    )
    user_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    objective: Mapped[str] = mapped_column(String(40))
    tactic_keys: Mapped[list] = mapped_column(ARRAY(Text))
    avoided_keys: Mapped[list] = mapped_column(ARRAY(Text), default=list)
    relationship_state_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("relationship_states.id")
    )
    preference_model_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True), nullable=True
    )
    skills_invoked: Mapped[list] = mapped_column(ARRAY(Text), default=list)
    rendered_text_sha256: Mapped[str] = mapped_column(String(64))
    rendered_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    n_candidates: Mapped[int] = mapped_column(Integer, default=1)
    critic: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    predicted_positive: Mapped[float] = mapped_column(Float)
    predicted_detail: Mapped[dict] = mapped_column(JSONB, default=dict)
    prediction_basis: Mapped[str] = mapped_column(Text)
    observation_window_s: Mapped[int] = mapped_column(Integer, default=259200)
    user_action: Mapped[str] = mapped_column(String(24), default="not_yet")
    shown_at: Mapped[datetime] = _ts(default=lambda: datetime.now(UTC))
    sent_at: Mapped[datetime | None] = _ts(nullable=True)
    model_version: Mapped[str] = mapped_column(String(64))


class StrategyOutcomeRow(Base):
    __tablename__ = "strategy_outcomes"
    id: Mapped[UUID] = _pk()
    strategy_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("strategy_records.id"), unique=True
    )
    relationship_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("relationships.id", ondelete="CASCADE"),
        index=True,
    )
    observed: Mapped[dict] = mapped_column(JSONB)
    label: Mapped[str] = mapped_column(OUTCOME_LABEL)
    evidence_interaction_ids: Mapped[list] = mapped_column(
        ARRAY(PgUUID(as_uuid=True)), default=list
    )
    window_start: Mapped[datetime] = _ts()
    window_end: Mapped[datetime] = _ts()
    confounders: Mapped[list] = mapped_column(ARRAY(Text), default=list)
    valid_for_learning: Mapped[bool] = mapped_column(Boolean, default=True)
    scorer_version: Mapped[str] = mapped_column(String(64))
    recorded_at: Mapped[datetime] = _ts(default=lambda: datetime.now(UTC))


class MemoryChunkRow(Base):
    __tablename__ = "memory_chunks"
    id: Mapped[UUID] = _pk()
    relationship_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("relationships.id", ondelete="CASCADE"),
        index=True,
    )
    kind: Mapped[str] = mapped_column(String(32))
    summary: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list | None] = mapped_column(Vector(1536), nullable=True)
    source_ids: Mapped[list] = mapped_column(ARRAY(PgUUID(as_uuid=True)), default=list)
    source_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    sensitivity: Mapped[str] = mapped_column(SENSITIVITY, default="standard")
    created_at: Mapped[datetime] = _ts(default=lambda: datetime.now(UTC))

    __table_args__ = (
        CheckConstraint(
            "cardinality(source_ids) = 0 OR source_kind IS NOT NULL",
            name="memory_sources_are_typed",
        ),
    )


class ConsentRecordRow(Base):
    __tablename__ = "consent_records"
    id: Mapped[UUID] = _pk()
    relationship_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("relationships.id", ondelete="CASCADE"),
        index=True,
    )
    activity: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(CONSENT_STATUS)
    provenance: Mapped[str] = mapped_column(PROVENANCE)
    stated_at: Mapped[datetime] = _ts()
    stated_by: Mapped[str] = mapped_column(String(16))
    verbatim_ref: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    recorded_at: Mapped[datetime] = _ts(default=lambda: datetime.now(UTC))

    __table_args__ = (
        CheckConstraint(
            "provenance IN ('user_stated','target_stated')", name="no_inferred_consent"
        ),
        CheckConstraint(
            "status = 'unknown' OR verbatim_ref IS NOT NULL",
            name="known_consent_cites_statement",
        ),
    )


class ApiTokenRow(Base):
    """Bearer tokens, stored hashed."""

    __tablename__ = "api_tokens"
    id: Mapped[UUID] = _pk()
    user_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    token_sha256: Mapped[str] = mapped_column(String(64), unique=True)
    label: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _ts(default=lambda: datetime.now(UTC))
    last_used_at: Mapped[datetime | None] = _ts(nullable=True)
    revoked_at: Mapped[datetime | None] = _ts(nullable=True)


class TelegramAccountRow(Base):
    """Telegram identity -> account. Never inferred from message content."""

    __tablename__ = "telegram_accounts"
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    linked_at: Mapped[datetime] = _ts(default=lambda: datetime.now(UTC))
