"""Schema 4/5 -- PreferenceEvidence (+ its aggregate, PreferenceModel).

The doc treats these as one thing. They must be two:

  PreferenceEvidence -- one atomic, append-only observation.
  PreferenceModel    -- the Beta posterior the Preference Router reads.

Splitting them buys three things: the posterior can be recomputed when the
scorer changes, a single bad extraction can be retracted without corrupting
the belief, and "n" stays honest.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from enum import Enum
from uuid import UUID, uuid4

from pydantic import Field, model_validator

from .primitives import (
    BeliefStatus,
    BetaEstimate,
    DataSubject,
    Frozen,
    Provenance,
    Sensitivity,
    Versioned,
    utcnow,
)


class PreferenceDomain(str, Enum):
    FLIRT_MODE = "flirt_mode"  # teasing, role_contrast, subtext, ...
    COMMUNICATION = "communication"  # message length, call vs text, pacing
    ACTIVITY = "activity"  # date types
    TOPIC = "topic"
    PHYSICAL = "physical"  # vault tier
    SEXUAL = "sexual"  # vault tier
    KINK = "kink"  # vault tier

    @property
    def is_protected(self) -> bool:
        return self in {
            PreferenceDomain.PHYSICAL,
            PreferenceDomain.SEXUAL,
            PreferenceDomain.KINK,
        }


class Direction(str, Enum):
    POSITIVE = "positive"
    NEUTRAL = "neutral"
    NEGATIVE = "negative"


class PreferenceEvidence(Frozen):
    """'On 2026-08-14, X responded positively to role_contrast, in this context.'

    Context is mandatory. A tactic that worked at 11pm one-on-one is not
    evidence about the same tactic in a group chat at noon, and collapsing
    the two is how a preference model learns a person who does not exist.
    """

    id: UUID = Field(default_factory=uuid4)
    person_id: UUID
    relationship_id: UUID
    domain: PreferenceDomain
    key: str = Field(max_length=80)

    direction: Direction
    strength: float = Field(ge=0.0, le=1.0, default=0.5)
    provenance: Provenance

    observed_at: datetime
    source_interaction_id: UUID | None = None
    source_signal_ids: tuple[UUID, ...] = ()
    source_strategy_outcome_id: UUID | None = None

    # context -- the confounders that make or break the inference
    stage_at_time: str | None = Field(default=None, max_length=40)
    channel: str | None = Field(default=None, max_length=40)
    in_group_setting: bool = False
    known_confounders: tuple[str, ...] = Field(
        default=(),
        description="e.g. 'she was drinking', 'right after a fight', "
        "'I sent three messages first'. Excludes the row from learning "
        "when severe.",
    )
    excluded_from_learning: bool = False

    sensitivity: Sensitivity = Sensitivity.STANDARD
    data_subject: DataSubject = DataSubject.THIRD_PARTY
    recorded_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _guard(self) -> PreferenceEvidence:
        if self.domain.is_protected:
            if self.provenance not in {
                Provenance.USER_STATED,
                Provenance.TARGET_STATED,
            }:
                raise ValueError(
                    f"{self.domain.value} preference requires an explicit statement; "
                    "it may not be inferred from behaviour or personality"
                )
            if self.sensitivity is not Sensitivity.VAULT:
                raise ValueError(f"{self.domain.value} evidence must be vault-tier")
        if not (
            self.source_interaction_id
            or self.source_signal_ids
            or self.source_strategy_outcome_id
            or self.provenance in {Provenance.USER_STATED, Provenance.TARGET_STATED}
        ):
            raise ValueError("preference evidence must cite a source")
        return self

    @property
    def counts_toward_learning(self) -> bool:
        return not self.excluded_from_learning


class PreferenceEntry(Frozen):
    key: str = Field(max_length=80)
    domain: PreferenceDomain
    estimate: BetaEstimate
    last_observed_at: datetime | None = None
    contributing_evidence_ids: tuple[UUID, ...] = ()

    @property
    def status(self) -> BeliefStatus:
        return self.estimate.status

    def statement(self) -> str:
        """The only phrasing the generator may use for this preference."""
        if self.status is BeliefStatus.UNKNOWN:
            return f"{self.key}: unknown"
        return f"{self.key}: {self.estimate.describe()}"


class UserTacticPrior(Frozen):
    """The only thing allowed to cross between relationships.

    Beta parameters for a tactic key and nothing else -- no person, no text,
    no context. It seeds BetaEstimate.prior_alpha/prior_beta so a new
    relationship starts from the user's own base rate rather than a flat
    prior, and is overwhelmed by person-level evidence as n grows.

    Requires k >= 3 distinct relationships. Below that this degenerates into
    "what worked on my ex, applied to her", which is the cross-person form of
    Preference Hallucination and is worse than having no prior at all.
    """

    user_id: UUID
    tactic_key: str = Field(max_length=80)
    domain: PreferenceDomain
    prior_alpha: float = Field(gt=0)
    prior_beta: float = Field(gt=0)
    n_relationships: int = Field(ge=3)
    computed_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _never_protected(self) -> UserTacticPrior:
        if self.domain.is_protected:
            raise ValueError(
                f"{self.domain.value} never forms a cross-person prior, at any k"
            )
        return self


class PreferenceModel(Versioned):
    """Aggregate over PreferenceEvidence. Always recomputable from scratch.

    Strictly dyad-scoped. There is no global preference model: the moment one
    exists, A's evidence starts steering B's suggestions.
    """

    id: UUID = Field(default_factory=uuid4)
    person_id: UUID
    relationship_id: UUID
    entries: dict[str, PreferenceEntry] = Field(default_factory=dict)
    scorer_version: str = Field(max_length=64)
    computed_at: datetime = Field(default_factory=utcnow)

    def get(self, key: str) -> PreferenceEntry | None:
        return self.entries.get(key)

    def recommended(self, min_status: BeliefStatus = BeliefStatus.EMERGING) -> list[str]:
        order = {
            BeliefStatus.UNKNOWN: 0,
            BeliefStatus.EMERGING: 1,
            BeliefStatus.ESTABLISHED: 2,
        }
        return sorted(
            (
                k
                for k, e in self.entries.items()
                if order[e.status] >= order[min_status] and e.estimate.mean > 0.55
            ),
            key=lambda k: self.entries[k].estimate.mean,
            reverse=True,
        )

    def avoid(self) -> list[str]:
        return [
            k
            for k, e in self.entries.items()
            if e.status is not BeliefStatus.UNKNOWN and e.estimate.mean < 0.35
        ]

    @classmethod
    def build(
        cls,
        person_id: UUID,
        evidence: list[PreferenceEvidence],
        scorer_version: str,
        relationship_id: UUID,
        version: int = 1,
        priors: dict[str, UserTacticPrior] | None = None,
    ) -> PreferenceModel:
        buckets: dict[tuple[str, PreferenceDomain], list[PreferenceEvidence]] = (
            defaultdict(list)
        )
        for e in evidence:
            if e.counts_toward_learning:
                buckets[(e.key, e.domain)].append(e)

        priors = priors or {}
        entries: dict[str, PreferenceEntry] = {}
        for (key, domain), rows in buckets.items():
            if any(r.relationship_id != relationship_id for r in rows):
                raise ValueError(
                    f"evidence for '{key}' comes from another relationship; "
                    "preference models are strictly dyad-scoped"
                )
            pos = sum(1 for r in rows if r.direction is Direction.POSITIVE)
            neg = sum(1 for r in rows if r.direction is Direction.NEGATIVE)
            neu = sum(1 for r in rows if r.direction is Direction.NEUTRAL)
            prior = priors.get(key)
            entries[key] = PreferenceEntry(
                key=key,
                domain=domain,
                estimate=BetaEstimate(
                    positive=pos,
                    negative=neg,
                    neutral=neu,
                    prior_alpha=prior.prior_alpha if prior else 1.0,
                    prior_beta=prior.prior_beta if prior else 1.0,
                ),
                last_observed_at=max(r.observed_at for r in rows),
                contributing_evidence_ids=tuple(r.id for r in rows),
            )
        return cls(
            version=version,
            person_id=person_id,
            relationship_id=relationship_id,
            entries=entries,
            scorer_version=scorer_version,
        )
