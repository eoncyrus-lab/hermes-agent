"""Shared primitives for the five core schemas.

Design rules encoded here (not conventions -- enforced by validators):

1. Anything the model INFERRED must carry evidence. An Assessment with
   provenance=MODEL_INFERENCE and no evidence refs is invalid.
2. Inference confidence is capped by how much evidence supports it.
   This is the schema-level defence against the two headline failure
   metrics: False Positive Attraction and Preference Hallucination.
3. Repeated-trial beliefs are Beta posteriors with an explicit n, never
   bare floats. "0.88 confidence 0.82" hides whether n=2 or n=40.
4. Every record declares a sensitivity tier, which drives storage routing
   (standard table / sensitive column / encrypted vault).
5. Every record about a third party declares that fact, so retention and
   deletion can be enforced per data subject.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Generic, Literal, TypeVar
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

T = TypeVar("T")


def utcnow() -> datetime:
    return datetime.now(UTC)


class Frozen(BaseModel):
    """Base for append-only records. Mutation is a bug, not a workflow."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class Mutable(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------
# provenance / sensitivity
# --------------------------------------------------------------------------


class Provenance(str, Enum):
    """Where a claim came from. Ordered loosely by trustworthiness."""

    USER_STATED = "user_stated"  # the user told us about themselves
    TARGET_STATED = "target_stated"  # the other person said it, in their own words
    OBSERVED_BEHAVIOR = "observed"  # extracted from actual interaction data
    MODEL_INFERENCE = "model_inference"  # the agent concluded it
    IMPORTED = "imported"  # bulk import, unverified


DIRECT_PROVENANCE = {Provenance.USER_STATED, Provenance.TARGET_STATED}


class Sensitivity(str, Enum):
    STANDARD = "standard"  # ordinary conversation / relationship data
    SENSITIVE = "sensitive"  # health, conflict, identity; encrypted column
    VAULT = "vault"  # sexual, kink, images; separate keyed store (doc s45)


class DataSubject(str, Enum):
    SELF = "self"
    THIRD_PARTY = "third_party"  # a real person who has not consented to profiling


# --------------------------------------------------------------------------
# evidence
# --------------------------------------------------------------------------


class EvidenceKind(str, Enum):
    MESSAGE = "message"
    INTERACTION = "interaction"
    SIGNAL = "signal"
    OUTCOME = "outcome"
    USER_STATEMENT = "user_statement"
    OFFLINE_EVENT = "offline_event"


class EvidenceRef(Frozen):
    kind: EvidenceKind
    ref_id: UUID
    occurred_at: datetime
    note: str | None = Field(default=None, max_length=280)

    @property
    def identity(self) -> tuple[EvidenceKind, UUID]:
        """Stable identity independent of copied timestamp or annotation metadata."""
        return (self.kind, self.ref_id)


class Alternative(Frozen):
    """A competing explanation. Required for low-confidence inferences.

    doc s10/s13: never emit a single interpretation as fact.
    """

    hypothesis: str = Field(max_length=280)
    plausibility: float = Field(ge=0.0, le=1.0)


# --------------------------------------------------------------------------
# assessment
# --------------------------------------------------------------------------


# Confidence ceilings for model inference, by evidence count. An inference
# backed by one message cannot be asserted at 0.9. This is the single most
# load-bearing rule in the schema layer.
class ConfidenceCeilingPolicy(Frozen):
    """Versioned metadata for the evidence-count confidence guard.

    Policy changes alter the meaning of stored confidence values, so the
    version is a first-class value suitable for calibration observations.
    """

    name: str = "evidence-count-ceiling"
    version: str
    thresholds: tuple[tuple[int, float], ...]

    @model_validator(mode="after")
    def _validate_thresholds(self) -> ConfidenceCeilingPolicy:
        counts = [count for count, _ in self.thresholds]
        caps = [cap for _, cap in self.thresholds]
        if not self.thresholds or counts[0] != 0:
            raise ValueError("confidence ceiling policy must start at 0 evidence")
        if counts != sorted(set(counts)):
            raise ValueError("confidence ceiling thresholds must be unique and ordered")
        if any(cap < 0.0 or cap > 1.0 for cap in caps):
            raise ValueError("confidence ceiling caps must be within [0, 1]")
        if caps != sorted(caps):
            raise ValueError("confidence ceiling caps must be non-decreasing")
        return self


CONFIDENCE_CEILING_POLICY = ConfidenceCeilingPolicy(
    version="confidence-ceiling/1.0.0",
    thresholds=(
        (0, 0.00),
        (1, 0.45),
        (2, 0.60),
        (4, 0.75),
        (8, 0.85),
        (16, 0.95),
    ),
)
CONFIDENCE_CEILING_POLICY_VERSION = CONFIDENCE_CEILING_POLICY.version


def confidence_ceiling(
    n_evidence: int, policy: ConfidenceCeilingPolicy = CONFIDENCE_CEILING_POLICY
) -> float:
    ceiling = 0.0
    for threshold, cap in policy.thresholds:
        if n_evidence >= threshold:
            ceiling = cap
    return ceiling


class Assessment(BaseModel, Generic[T]):
    """A single believed value plus everything needed to audit it.

    Every derived field in PersonProfile and RelationshipState is one of
    these. That is what makes "why does the agent think her interest went
    up?" answerable (doc s47).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    value: T
    confidence: float = Field(ge=0.0, le=1.0)
    provenance: Provenance
    evidence: tuple[EvidenceRef, ...] = ()
    counter_evidence: tuple[EvidenceRef, ...] = ()
    alternatives: tuple[Alternative, ...] = ()
    prior: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Base rate before this evidence. Forces s59 calibration: "
        "politeness is the base rate, not a signal.",
    )
    method: str | None = Field(default=None, max_length=120)
    model_version: str | None = Field(default=None, max_length=64)
    as_of: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _enforce_evidence_rules(self) -> Assessment[T]:
        refs = (*self.evidence, *self.counter_evidence)
        identities = [ref.identity for ref in refs]
        if len(identities) != len(set(identities)):
            raise ValueError(
                "Assessment contains duplicate evidence identity (kind, ref_id)"
            )
        if self.provenance is Provenance.MODEL_INFERENCE:
            n = len(self.evidence)
            if n == 0:
                raise ValueError(
                    "MODEL_INFERENCE assessment requires at least one EvidenceRef"
                )
            cap = confidence_ceiling(n)
            if self.confidence > cap:
                raise ValueError(
                    f"confidence {self.confidence:.2f} exceeds ceiling {cap:.2f} "
                    f"for {n} evidence item(s)"
                )
            if self.confidence < 0.7 and not self.alternatives:
                raise ValueError(
                    "low-confidence inference must carry at least one alternative "
                    "hypothesis"
                )
        return self

    @property
    def is_actionable(self) -> bool:
        """Whether strategy may condition on this. Below 0.6 -> ask, don't assume."""
        return self.confidence >= 0.6


# --------------------------------------------------------------------------
# repeated-trial belief
# --------------------------------------------------------------------------


class BeliefStatus(str, Enum):
    UNKNOWN = "unknown"  # n below floor; agent must say "unknown"
    EMERGING = "emerging"  # directional, must be hedged in output
    ESTABLISHED = "established"


class BetaEstimate(Frozen):
    """Posterior over 'does this person respond positively to X'.

    Replaces doc s17's bare `response_score: 0.88`. The interval is what
    stops the agent claiming a preference from three data points.
    """

    positive: int = Field(ge=0)
    negative: int = Field(ge=0)
    neutral: int = Field(ge=0, default=0)
    prior_alpha: float = Field(default=1.0, gt=0)
    prior_beta: float = Field(default=1.0, gt=0)
    established_at_n: int = Field(default=8, ge=1)
    emerging_at_n: int = Field(default=3, ge=1)

    @property
    def n(self) -> int:
        return self.positive + self.negative + self.neutral

    @property
    def alpha(self) -> float:
        return self.prior_alpha + self.positive + 0.5 * self.neutral

    @property
    def beta(self) -> float:
        return self.prior_beta + self.negative + 0.5 * self.neutral

    @property
    def mean(self) -> float:
        return self.alpha / (self.alpha + self.beta)

    @property
    def stddev(self) -> float:
        a, b = self.alpha, self.beta
        return math.sqrt((a * b) / ((a + b) ** 2 * (a + b + 1)))

    def interval(self, z: float = 1.96) -> tuple[float, float]:
        lo = max(0.0, self.mean - z * self.stddev)
        hi = min(1.0, self.mean + z * self.stddev)
        return lo, hi

    @property
    def status(self) -> BeliefStatus:
        if self.n >= self.established_at_n:
            return BeliefStatus.ESTABLISHED
        if self.n >= self.emerging_at_n:
            return BeliefStatus.EMERGING
        return BeliefStatus.UNKNOWN

    def describe(self) -> str:
        """The only string the agent is allowed to state about this belief."""
        if self.status is BeliefStatus.UNKNOWN:
            return f"unknown (n={self.n})"
        lo, hi = self.interval()
        hedge = "likely" if self.status is BeliefStatus.EMERGING else "consistently"
        return f"{hedge} {self.mean:.0%} [{lo:.0%}-{hi:.0%}] (n={self.n})"


# --------------------------------------------------------------------------
# consent -- deliberately not inferable
# --------------------------------------------------------------------------


class ConsentStatus(str, Enum):
    """doc s39. Note there is no 'likely'. Attraction never implies consent."""

    CONFIRMED = "confirmed"
    DECLINED = "declined"
    HARD_LIMIT = "hard_limit"
    UNKNOWN = "unknown"


class ConsentRecord(Frozen):
    """Per-activity consent. The model may never author one of these.

    A ConsentRecord requires a direct statement by one of the two people.
    OBSERVED_BEHAVIOR is rejected too: reading consent off behaviour is the
    exact error this type exists to prevent.
    """

    id: UUID = Field(default_factory=uuid4)
    relationship_id: UUID
    activity: str = Field(max_length=120)
    status: ConsentStatus
    provenance: Provenance
    stated_at: datetime
    stated_by: Literal["self", "target"]
    verbatim_ref: EvidenceRef | None = None
    revocable_note: str = Field(
        default="Consent is revocable at any time and is scoped to this activity.",
        max_length=240,
    )
    sensitivity: Sensitivity = Sensitivity.VAULT
    recorded_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _no_inferred_consent(self) -> ConsentRecord:
        if self.provenance not in DIRECT_PROVENANCE:
            raise ValueError(
                "consent may only originate from an explicit statement "
                "(USER_STATED or TARGET_STATED); "
                f"got {self.provenance.value}"
            )
        if self.status is not ConsentStatus.UNKNOWN and self.verbatim_ref is None:
            raise ValueError("non-unknown consent must cite the statement it came from")
        return self


class Versioned(BaseModel):
    """Snapshot semantics: never overwrite, always supersede (doc s48)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: int = Field(ge=1)
    supersedes: UUID | None = None
    created_at: datetime = Field(default_factory=utcnow)


def scalar(
    value: Any,
    confidence: float,
    provenance: Provenance = Provenance.MODEL_INFERENCE,
    **kw: Any,
) -> Assessment[Any]:
    """Convenience constructor used by extractors."""
    return Assessment[Any](
        value=value, confidence=confidence, provenance=provenance, **kw
    )
