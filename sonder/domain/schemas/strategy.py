"""Schema 5/5 -- StrategyRecord + StrategyOutcome.

The doc models this as one object holding `prediction` and `actual`. That
cannot work: if both are written after the fact, nothing stops the
prediction being reconstructed to match, and the calibration numbers become
decoration.

So it is two records with a hard time ordering:

  StrategyRecord  -- written when the suggestion is shown. Frozen. Contains
                     the prediction and nothing about what happened.
  StrategyOutcome -- written >= observation window later. References the
                     record; the record is never touched again.

That ordering is what turns the outcome table into the asset in doc s68.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import Enum
from uuid import UUID, uuid4

from pydantic import Field, computed_field, model_validator

from .primitives import Frozen, Sensitivity, utcnow


class Objective(str, Enum):
    """doc s28: decide the goal before the words."""

    ESTABLISH_RAPPORT = "establish_rapport"
    INCREASE_ENGAGEMENT = "increase_engagement"
    DEEPEN_DISCLOSURE = "deepen_disclosure"
    BUILD_TENSION = "build_tension"
    MOVE_OFFLINE = "move_offline"
    ADVANCE_PHYSICAL = "advance_physical"
    DEFINE_RELATIONSHIP = "define_relationship"
    REPAIR = "repair"
    DE_ESCALATE = "de_escalate"
    CLARIFY_INTENT = "clarify_intent"
    DISENGAGE = "disengage"


class UserAction(str, Enum):
    SENT_VERBATIM = "sent_verbatim"
    SENT_EDITED = "sent_edited"
    DISCARDED = "discarded"
    NOT_YET = "not_yet"


class CriticScores(Frozen):
    """doc s55. Stored, not just used as a gate, so the Critic is auditable."""

    naturalness: float = Field(ge=0, le=1)
    voice_match: float = Field(ge=0, le=1)
    context_match: float = Field(ge=0, le=1)
    strategy_match: float = Field(ge=0, le=1)
    tension: float | None = Field(default=None, ge=0, le=1)
    cringe_risk: float = Field(ge=0, le=1)
    ai_pattern_risk: float = Field(ge=0, le=1)
    pressure: float = Field(ge=0, le=1)
    manipulation_risk: float = Field(ge=0, le=1)
    boundary_risk: float = Field(ge=0, le=1)
    cross_dyad_leak_risk: float = Field(
        default=0.0,
        ge=0,
        le=1,
        description="Compatibility sentinel. Production sets this to 0 when "
        "cross-dyad comparison is unavailable; zero must not be interpreted "
        "as 'compared and clean'. A non-zero score is only produced when an "
        "explicit, safely authenticated phrase set is supplied.",
    )

    @property
    def hard_rejected(self) -> bool:
        return (
            self.manipulation_risk > 0.5
            or self.boundary_risk > 0.5
            or self.cross_dyad_leak_risk > 0.05
        )


class StrategyRecord(Frozen):
    id: UUID = Field(default_factory=uuid4)
    relationship_id: UUID
    user_id: UUID

    objective: Objective
    tactic_keys: tuple[str, ...] = Field(
        min_length=1,
        description="Preference-model keys this action exercises. The join "
        "column between strategy and preference learning.",
    )
    avoided_keys: tuple[str, ...] = ()

    # state as it was believed at decision time, so a wrong call can later be
    # attributed to a bad read vs a bad tactic
    relationship_state_id: UUID
    preference_model_id: UUID | None = None
    skills_invoked: tuple[str, ...] = ()

    rendered_text_sha256: str = Field(max_length=64)
    rendered_text: str | None = Field(default=None, repr=False)
    n_candidates: int = Field(default=1, ge=1)
    critic: CriticScores | None = None

    # --- the prediction, written before anything is observed ---
    predicted_positive: float = Field(
        ge=0.0, le=1.0, description="P(positive outcome). Immutable. Brier-scored."
    )
    predicted_detail: dict[str, float] = Field(default_factory=dict)
    prediction_basis: str = Field(
        max_length=280,
        description="One line: which evidence drove the number. Empty is a bug.",
    )

    observation_window: timedelta = Field(default=timedelta(hours=72))
    user_action: UserAction = UserAction.NOT_YET
    shown_at: datetime = Field(default_factory=utcnow)
    sent_at: datetime | None = None
    model_version: str = Field(max_length=64)
    sensitivity: Sensitivity = Sensitivity.STANDARD

    @model_validator(mode="after")
    def _check(self) -> StrategyRecord:
        if not self.prediction_basis.strip():
            raise ValueError("prediction_basis is required")
        if self.critic and self.critic.hard_rejected:
            raise ValueError("hard-rejected candidate must not be recorded as shown")
        if self.user_action in {UserAction.SENT_VERBATIM, UserAction.SENT_EDITED} and (
            self.sent_at is None
        ):
            raise ValueError("a sent strategy needs sent_at")
        return self


class OutcomeLabel(str, Enum):
    POSITIVE = "positive"
    NEUTRAL = "neutral"
    NEGATIVE = "negative"
    INDETERMINATE = "indeterminate"  # not enough happened to judge


class ObservedOutcome(Frozen):
    """doc s42: never score on 'she replied fast' alone.

    Each field is a delta against that person's own recent baseline, not an
    absolute, and each is optional -- absent means unobserved, not zero.
    """

    reply_latency_delta: float | None = Field(
        default=None, description="Negative = faster than her baseline."
    )
    reply_length_delta: float | None = None
    question_returned: bool | None = None
    topic_continued: bool | None = None
    flirt_reciprocated: bool | None = None
    disclosure_deepened: bool | None = None
    initiated_next_contact: bool | None = None
    plan_accepted: bool | None = None
    offline_progress: bool | None = None
    discomfort_or_boundary: bool | None = Field(
        default=None,
        description="Dominates. Any true value forces NEGATIVE regardless of "
        "every engagement metric.",
    )
    health_delta: float | None = Field(default=None, ge=-1, le=1)


class StrategyOutcome(Frozen):
    id: UUID = Field(default_factory=uuid4)
    strategy_id: UUID
    relationship_id: UUID

    observed: ObservedOutcome
    label: OutcomeLabel
    evidence_interaction_ids: tuple[UUID, ...] = ()
    window_start: datetime
    window_end: datetime

    confounders: tuple[str, ...] = Field(
        default=(),
        description="External events in the window: she was travelling, "
        "her exam week, we also met in person.",
    )
    valid_for_learning: bool = Field(
        default=True,
        description="False when the user rewrote the message, the window was "
        "contaminated, or the outcome is indeterminate. Invalid rows are kept "
        "for audit but excluded from the posterior.",
    )
    scorer_version: str = Field(max_length=64)
    recorded_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _check(self) -> StrategyOutcome:
        if self.window_end < self.window_start:
            raise ValueError("window_end precedes window_start")
        if (
            self.observed.discomfort_or_boundary
            and self.label is not OutcomeLabel.NEGATIVE
        ):
            raise ValueError(
                "an observed boundary or discomfort signal forces a NEGATIVE label"
            )
        if self.label is OutcomeLabel.INDETERMINATE and self.valid_for_learning:
            raise ValueError("indeterminate outcomes cannot train the model")
        return self

    def brier(self, record: StrategyRecord) -> float | None:
        """Calibration for one prediction. Mean over these is the s58 metric."""
        if record.id != self.strategy_id or not self.valid_for_learning:
            return None
        actual = {
            OutcomeLabel.POSITIVE: 1.0,
            OutcomeLabel.NEUTRAL: 0.5,
            OutcomeLabel.NEGATIVE: 0.0,
        }.get(self.label)
        if actual is None:
            return None
        return (record.predicted_positive - actual) ** 2

    @computed_field  # type: ignore[prop-decorator]
    @property
    def attributable(self) -> bool:
        """Whether this outcome may update the preference posterior at all."""
        return self.valid_for_learning and not self.confounders
