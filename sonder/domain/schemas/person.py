"""Schema 1/5 -- PersonProfile.

One type covers both the user and each target. The difference is the
`subject` field, which changes what is allowed to be written:

  SELF   -- facts may be USER_STATED; voice model is built here.
  TARGET -- a real person who has not consented to being profiled.
            Sexual/kink attributes may never be MODEL_INFERENCE.
            Carries a retention policy and is deletable as a unit.

That second rule is the schema-level form of doc s59's Preference
Hallucination metric: "she is assertive at work" can never become
"she likely prefers to be submissive".
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from pydantic import Field, model_validator

from .primitives import (
    Assessment,
    DataSubject,
    Provenance,
    Sensitivity,
    Versioned,
    utcnow,
)


class Subject(str, Enum):
    SELF = "self"
    TARGET = "target"


class VoiceProfile(Versioned):
    """doc s16. The user's actual register, measured, not described.

    Only ever built for Subject.SELF. Imitating a target's voice is not a
    product feature.
    """

    person_id: UUID
    n_samples: int = Field(default=0, ge=0)

    median_chars: float | None = Field(default=None, ge=0)
    p90_chars: float | None = Field(default=None, ge=0)
    messages_per_turn: float | None = Field(default=None, ge=0)
    emoji_rate: float | None = Field(default=None, ge=0, le=1)
    question_rate: float | None = Field(default=None, ge=0, le=1)
    terminal_punctuation_rate: float | None = Field(default=None, ge=0, le=1)
    ellipsis_rate: float | None = Field(default=None, ge=0, le=1)
    profanity_rate: float | None = Field(default=None, ge=0, le=1)
    lowercase_start_rate: float | None = Field(default=None, ge=0, le=1)

    recurring_phrases: tuple[str, ...] = ()
    slang_terms: tuple[str, ...] = ()
    avoided_constructions: tuple[str, ...] = Field(
        default=(),
        description="Turns of phrase this user demonstrably never writes. "
        "Feeds the Humanizer's penalty list (doc s27).",
    )
    exemplar_message_ids: tuple[UUID, ...] = ()
    embedding_ref: str | None = Field(default=None, max_length=200)

    @property
    def is_usable(self) -> bool:
        """Below this the Humanizer must fall back to neutral, not guess."""
        return self.n_samples >= 40


class RelationalHistoryNote(Versioned):
    """Self-model only: patterns the user has agreed are theirs."""

    person_id: UUID
    pattern: str = Field(max_length=400)
    acknowledged_by_user: bool = False
    provenance: Provenance = Provenance.USER_STATED
    sensitivity: Sensitivity = Sensitivity.SENSITIVE


# Attribute namespaces that a model may never author for a third party.
PROTECTED_NAMESPACES = ("sexual.", "kink.", "body.", "health.", "consent.")


class PersonProfile(Versioned):
    id: UUID = Field(default_factory=uuid4)
    user_id: UUID
    subject: Subject
    data_subject: DataSubject

    display_name: str = Field(max_length=80)
    is_pseudonym: bool = True

    # Plain facts. Wrapped in Assessment only where they were inferred.
    attributes: dict[str, Assessment[Any]] = Field(default_factory=dict)

    # Big-five-ish traits, each independently evidenced.
    traits: dict[str, Assessment[float]] = Field(default_factory=dict)

    # Pointer, not embedding: the aggregate lives in PreferenceModel.
    preference_model_id: UUID | None = None
    voice_profile_id: UUID | None = None

    first_seen_at: datetime | None = None
    last_interaction_at: datetime | None = None

    sensitivity: Sensitivity = Sensitivity.STANDARD
    retention_until: datetime | None = Field(
        default=None,
        description="Third-party profiles expire by default. Renewed by "
        "continued interaction, not by inertia.",
    )
    deletion_requested_at: datetime | None = None
    updated_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _guard_third_party_inference(self) -> PersonProfile:
        if (
            self.subject is Subject.TARGET
            and self.data_subject is not DataSubject.THIRD_PARTY
        ):
            raise ValueError("a TARGET profile is always THIRD_PARTY data")

        if self.data_subject is DataSubject.THIRD_PARTY:
            # Mirrors the third_party_expires CHECK constraint. A profile of
            # someone who never agreed to be modelled does not get to sit
            # there indefinitely because nobody remembered to set a date.
            if self.retention_until is None and self.deletion_requested_at is None:
                raise ValueError(
                    "a third-party profile requires retention_until; profiles of "
                    "people who have not consented to being modelled expire by "
                    "default"
                )
            for key, a in {**self.attributes, **self.traits}.items():
                if key.startswith(PROTECTED_NAMESPACES) and (
                    a.provenance is Provenance.MODEL_INFERENCE
                ):
                    raise ValueError(
                        f"attribute '{key}' is in a protected namespace and cannot "
                        "be model-inferred for a third party; it must be stated"
                    )
        return self

    def evidence_for(self, key: str) -> tuple[str, ...]:
        """Answers 'why do you think that?' for any single attribute."""
        a = self.attributes.get(key) or self.traits.get(key)
        if a is None:
            return ()
        return tuple(f"{e.kind.value}:{e.ref_id}" for e in a.evidence)
