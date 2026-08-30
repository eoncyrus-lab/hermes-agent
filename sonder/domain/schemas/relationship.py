"""Schema 2/5 -- RelationshipState.

A versioned, immutable snapshot of one dyad at one moment. Never updated in
place (doc s48): trend is the whole point, and you cannot draw a trend
through overwritten rows.

Two structural choices worth defending:

1. `stage` is derived and marked as such. It exists for the UI sentence
   ("暧昧期"), and no strategy code may branch on it. Strategy reads the
   dimensions. Otherwise the eight-stage ladder quietly becomes the model
   again, and every nuance collapses back into it.

2. `health` is not a component of `attraction`, and neither feeds the other.
   Chemistry rising while health falls is a real and important state
   (doc s30), and it is only representable if the two are independent.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from uuid import UUID, uuid4

from pydantic import Field, computed_field, model_validator

from .primitives import Assessment, Frozen, Sensitivity, Versioned, utcnow

Score = Assessment[float]

# Attraction must clear Assessment.is_actionable (0.6) before strategy may use
# it: a false positive there is the product's headline failure mode.
#
# Safety findings use a deliberately lower bar. The costs are not symmetric --
# a false positive on "she seemed uncomfortable" costs the user one cautious
# message; a false negative costs the other person something real. Warning
# early is the cheaper error, so it is allowed to fire on weaker evidence.
SAFETY_CONFIDENCE_FLOOR = 0.35


class Stage(str, Enum):
    """UI summary only. Never a strategy input."""

    S0_STRANGER = "s0_stranger"
    S1_FAMILIARITY = "s1_familiarity"
    S2_ACTIVE = "s2_active_interaction"
    S3_PERSONAL = "s3_personal_connection"
    S4_FLIRTING = "s4_flirting"
    S5_MUTUAL = "s5_mutual_attraction"
    S6_DATING = "s6_dating"
    S7_RELATIONSHIP = "s7_relationship"
    SX_COOLING = "sx_cooling"
    SX_ENDED = "sx_ended"


class AttractionAxes(Frozen):
    romantic: Score | None = None
    sexual: Score | None = None
    intellectual: Score | None = None
    emotional: Score | None = None


class IntimacyAxes(Frozen):
    conversational: Score | None = None
    emotional: Score | None = None
    physical: Score | None = None
    sexual: Score | None = None


class BondAxes(Frozen):
    comfort: Score | None = None
    trust: Score | None = None
    reciprocity: Score | None = None
    commitment: Score | None = None


class DynamicsAxes(Frozen):
    initiative_balance: Score | None = Field(
        default=None, description="0 = all self, 1 = all target."
    )
    momentum: Score | None = None
    uncertainty: Score | None = Field(
        default=None,
        description="The agent's own ignorance, not her ambivalence. High "
        "uncertainty must suppress confident strategy, not be smoothed away.",
    )


class HealthAxes(Frozen):
    """doc s30. Deliberately includes the user's own conduct, not only hers."""

    mutual_respect: Score | None = None
    autonomy_preserved: Score | None = None
    emotional_safety: Score | None = None
    pressure_exerted_by_self: Score | None = Field(
        default=None, description="Higher is worse. Surfaced to the user unprompted."
    )
    dependence_asymmetry: Score | None = None


class CompatibilityAxes(Frozen):
    short_term: Score | None = None
    long_term: Score | None = None
    sexual: Score | None = Field(default=None)
    unassessed_domains: tuple[str, ...] = Field(
        default=(),
        description="Named gaps. An unassessed domain is reported as a gap, "
        "never silently scored as neutral.",
    )


class RelationshipState(Versioned):
    id: UUID = Field(default_factory=uuid4)
    relationship_id: UUID
    user_id: UUID
    target_person_id: UUID

    as_of: datetime
    computed_from_interaction_ids: tuple[UUID, ...] = ()
    window_start: datetime | None = None
    window_end: datetime | None = None

    attraction: AttractionAxes = AttractionAxes()
    intimacy: IntimacyAxes = IntimacyAxes()
    bond: BondAxes = BondAxes()
    dynamics: DynamicsAxes = DynamicsAxes()
    health: HealthAxes = HealthAxes()
    compatibility: CompatibilityAxes = CompatibilityAxes()

    stage: Stage = Stage.S0_STRANGER
    stage_is_derived: bool = Field(default=True, frozen=True)

    model_version: str = Field(max_length=64)
    sensitivity: Sensitivity = Sensitivity.STANDARD
    computed_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _require_provenance(self) -> RelationshipState:
        if self.version > 1 and self.supersedes is None:
            raise ValueError("version > 1 must name the snapshot it supersedes")
        if not self.computed_from_interaction_ids and self.version > 1:
            raise ValueError(
                "a recomputed state must cite the interactions it was computed from"
            )
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def health_flag(self) -> bool:
        """True when the agent owes the user an unprompted warning."""
        h = self.health
        risky = [h.mutual_respect, h.autonomy_preserved, h.emotional_safety]
        if any(
            s is not None and s.value < 0.55 and s.confidence >= SAFETY_CONFIDENCE_FLOOR
            for s in risky
        ):
            return True
        p = h.pressure_exerted_by_self
        return bool(
            p is not None and p.value > 0.35 and p.confidence >= SAFETY_CONFIDENCE_FLOOR
        )

    def actionable_axes(self) -> dict[str, float]:
        """Only axes the strategy layer is permitted to condition on."""
        out: dict[str, float] = {}
        for group_name in ("attraction", "intimacy", "bond", "dynamics"):
            group = getattr(self, group_name)
            for axis, val in group:
                if isinstance(val, Assessment) and val.is_actionable:
                    out[f"{group_name}.{axis}"] = val.value
        return out
