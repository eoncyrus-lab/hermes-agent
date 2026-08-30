"""Signals -> RelationshipState.

The whole design goal is to make the reading conservative by construction:

  * each axis has an explicit prior. attraction.romantic starts at 0.25
    because most warm, responsive interaction is friendliness, and a system
    that starts from 0.5 will read politeness as interest forever.
  * confidence is derived from evidence count and passed through the same
    ceiling the Assessment type enforces, so no axis can be asserted
    strongly from thin data.
  * every axis carries the signal ids it was computed from.
  * signals are weighted by their own confidence, so a hedged extraction
    moves the state less than a certain one.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import UTC, datetime
from uuid import UUID

from ..domain.schemas.interaction import Signal, SignalType, Speaker
from ..domain.schemas.primitives import (
    Alternative,
    Assessment,
    EvidenceKind,
    EvidenceRef,
    Provenance,
    confidence_ceiling,
)
from ..domain.schemas.relationship import (
    AttractionAxes,
    BondAxes,
    CompatibilityAxes,
    DynamicsAxes,
    HealthAxes,
    IntimacyAxes,
    RelationshipState,
    Stage,
)

MODEL_VERSION = "state/1.0.0"

# Priors. These are the base rates that "she is just being polite" lives in.
PRIORS: dict[str, float] = {
    "attraction.romantic": 0.25,
    "attraction.sexual": 0.15,
    "attraction.intellectual": 0.40,
    "attraction.emotional": 0.30,
    "intimacy.conversational": 0.30,
    "intimacy.emotional": 0.20,
    "intimacy.physical": 0.05,
    "intimacy.sexual": 0.05,
    "bond.comfort": 0.35,
    "bond.trust": 0.30,
    "bond.reciprocity": 0.50,
    "bond.commitment": 0.10,
    "dynamics.initiative_balance": 0.50,
    "dynamics.momentum": 0.50,
    "health.mutual_respect": 0.75,
    "health.autonomy_preserved": 0.80,
    "health.emotional_safety": 0.75,
    "health.pressure_exerted_by_self": 0.10,
}

# Which axes a signal informs, and how hard it pushes. Sparse on purpose:
# a signal that does not clearly bear on an axis should not touch it.
WEIGHTS: dict[SignalType, dict[str, float]] = {
    SignalType.ENGAGEMENT_UP: {
        "intimacy.conversational": 0.5,
        "dynamics.momentum": 0.6,
    },
    SignalType.ENGAGEMENT_DOWN: {
        "intimacy.conversational": -0.5,
        "dynamics.momentum": -0.7,
    },
    SignalType.INITIATIVE: {
        "dynamics.initiative_balance": 0.7,
        "attraction.romantic": 0.2,
    },
    SignalType.RECIPROCATION: {"bond.reciprocity": 0.6},
    SignalType.SELF_DISCLOSURE: {
        "intimacy.emotional": 0.7,
        "bond.trust": 0.5,
    },
    SignalType.PLAYFUL_CHALLENGE: {"intimacy.conversational": 0.3},
    SignalType.TEASING_RETURNED: {
        "attraction.romantic": 0.35,
        "intimacy.conversational": 0.5,
    },
    SignalType.WARMTH: {"bond.comfort": 0.5, "attraction.emotional": 0.2},
    SignalType.AVOIDANCE: {
        "dynamics.momentum": -0.6,
        "attraction.romantic": -0.4,
    },
    SignalType.BOUNDARY_SET: {
        "health.autonomy_preserved": 0.3,
        "intimacy.physical": -0.3,
    },
    SignalType.PLAN_ACCEPTED: {
        "attraction.romantic": 0.45,
        "bond.commitment": 0.3,
        "dynamics.momentum": 0.5,
    },
    SignalType.PLAN_DEFLECTED: {
        "attraction.romantic": -0.4,
        "dynamics.momentum": -0.4,
    },
    SignalType.CONFLICT: {"health.emotional_safety": -0.4, "bond.comfort": -0.3},
    SignalType.REPAIR_ATTEMPT: {"bond.trust": 0.4, "health.emotional_safety": 0.3},
    SignalType.FLIRT_INITIATED: {"attraction.romantic": 0.3},
    SignalType.FLIRT_RECIPROCATED: {
        "attraction.romantic": 0.6,
        "attraction.sexual": 0.35,
    },
    SignalType.DISCOMFORT: {
        "health.emotional_safety": -0.6,
        "health.pressure_exerted_by_self": 0.5,
    },
}

# Signals that say something about the USER's conduct, not the target's.
SELF_CONDUCT = {SignalType.DISCOMFORT, SignalType.BOUNDARY_SET}

ALTERNATIVES: dict[str, str] = {
    "attraction.romantic": "ordinary friendliness or politeness",
    "attraction.sexual": "warmth being read as sexual interest",
    "intimacy.emotional": "she talks this way with everyone",
    "dynamics.momentum": "a temporary change in her schedule",
    "bond.reciprocity": "she is matching effort out of courtesy",
}

HALF_LIFE_DAYS = 21.0

# Axes where under-reacting is the more expensive error. Shrinkage toward the
# prior is what keeps attraction readings honest, but applying the same
# damping to a discomfort signal buries it, so these get far less of it.
SAFETY_AXES = {
    "health.mutual_respect",
    "health.autonomy_preserved",
    "health.emotional_safety",
    "health.pressure_exerted_by_self",
}


def input_fingerprint(signals: list[Signal], interaction_ids: list[UUID]) -> str:
    """Stable identity of the immutable inputs used for one state snapshot."""
    payload = {
        "model_version": MODEL_VERSION,
        "interactions": sorted(str(value) for value in set(interaction_ids)),
        "signals": sorted(
            (
                str(signal.interaction_id),
                signal.type.value,
                signal.by.value,
                signal.occurred_at.isoformat(),
                signal.magnitude,
                signal.confidence,
                sorted(str(value) for value in set(signal.evidence_message_ids)),
                signal.extractor_version,
            )
            for signal in signals
        ),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _decay(occurred: datetime, now: datetime) -> float:
    age = max(0.0, (now - occurred).total_seconds() / 86400)
    return 0.5 ** (age / HALF_LIFE_DAYS)


def _signal_evidence_identity(signal: Signal) -> tuple:
    """Identity of the source evidence, independent of the Signal row UUID."""
    return (
        signal.relationship_id,
        signal.interaction_id,
        signal.type,
        signal.by,
        tuple(sorted(set(signal.evidence_message_ids), key=str)),
    )


def _axis(
    key: str,
    contributions: list[tuple[float, float, EvidenceRef]],
) -> Assessment[float] | None:
    """Combine weighted signal contributions into one auditable Assessment."""
    if not contributions:
        return None
    prior = PRIORS.get(key, 0.5)
    total_w = sum(abs(w) for w, _, _ in contributions)
    if total_w == 0:
        return None
    damping = 0.25 if key in SAFETY_AXES else 2.0
    shift = sum(w * c for w, c, _ in contributions) / max(1.0, total_w + damping)
    value = max(0.0, min(1.0, prior + shift))

    evidence = tuple(ref for _, _, ref in contributions)
    n = len(evidence)
    mean_conf = sum(c for _, c, _ in contributions) / n
    ramp = min(1.0, n / (2.0 if key in SAFETY_AXES else 6.0))
    confidence = min(confidence_ceiling(n), mean_conf * ramp)
    confidence = round(max(0.05, confidence), 3)

    alts: tuple[Alternative, ...] = ()
    if confidence < 0.7:
        hypothesis = ALTERNATIVES.get(key, "insufficient evidence to distinguish")
        alts = (
            Alternative(hypothesis=hypothesis, plausibility=round(1 - confidence, 2)),
        )

    return Assessment[float](
        value=round(value, 3),
        confidence=confidence,
        provenance=Provenance.MODEL_INFERENCE,
        evidence=evidence,
        alternatives=alts,
        prior=prior,
        method="weighted-signal-aggregation",
        model_version=MODEL_VERSION,
    )


def derive_stage(axes: dict[str, Assessment[float]]) -> Stage:
    """UI label only. Nothing downstream branches on this."""

    def v(k: str, default: float = 0.0) -> float:
        a = axes.get(k)
        return a.value if a and a.is_actionable else default

    if v("bond.commitment") > 0.6:
        return Stage.S7_RELATIONSHIP
    if v("attraction.romantic") > 0.6 and v("intimacy.conversational") > 0.5:
        return Stage.S5_MUTUAL
    if v("attraction.romantic") > 0.45:
        return Stage.S4_FLIRTING
    if v("intimacy.emotional") > 0.4:
        return Stage.S3_PERSONAL
    if v("intimacy.conversational") > 0.4:
        return Stage.S2_ACTIVE
    # Cooling is a claim, so it needs positive evidence of decline. Absence of
    # actionable evidence is ignorance, not a downturn, and reporting it as
    # one would have the agent tell every new user they are being iced out.
    momentum = axes.get("dynamics.momentum")
    if momentum is not None and momentum.is_actionable and momentum.value < 0.3:
        return Stage.SX_COOLING
    return Stage.S1_FAMILIARITY


def compute(
    signals: list[Signal],
    *,
    relationship_id: UUID,
    user_id: UUID,
    target_person_id: UUID,
    version: int,
    supersedes: UUID | None,
    interaction_ids: list[UUID],
    now: datetime | None = None,
    unassessed_compat: tuple[str, ...] = (
        "values",
        "life_goals",
        "location",
        "finances",
        "family",
    ),
) -> RelationshipState:
    now = now or datetime.now(UTC)
    buckets: dict[str, list[tuple[float, float, EvidenceRef]]] = defaultdict(list)

    # Retries and joins can return the same immutable signal more than once.
    # Count it once: confidence ceilings are evidence-count based, so duplicate
    # rows must not manufacture certainty.
    unique_signals: list[Signal] = []
    seen_evidence: set[tuple] = set()
    for signal in signals:
        identity = _signal_evidence_identity(signal)
        if identity not in seen_evidence:
            seen_evidence.add(identity)
            unique_signals.append(signal)

    for sig in unique_signals:
        weights = WEIGHTS.get(sig.type, {})
        decay = _decay(sig.occurred_at, now)
        ref = EvidenceRef(
            kind=EvidenceKind.SIGNAL, ref_id=sig.id, occurred_at=sig.occurred_at
        )
        for axis, w in weights.items():
            # A signal about the target's behaviour speaks to her state; a
            # signal about the user's conduct speaks to relationship health.
            if (
                sig.by is Speaker.SELF
                and sig.type not in SELF_CONDUCT
                and not axis.startswith(("dynamics.", "health."))
            ):
                continue
            direction = 1.0 if sig.magnitude >= 0 else -1.0
            contribution = w * abs(sig.magnitude) * direction * decay
            buckets[axis].append((contribution, sig.confidence, ref))

    axes = {k: a for k, v in buckets.items() if (a := _axis(k, v)) is not None}

    def pick(prefix: str) -> dict[str, Assessment[float]]:
        return {
            k.split(".", 1)[1]: v for k, v in axes.items() if k.startswith(prefix + ".")
        }

    window = [s.occurred_at for s in unique_signals]
    return RelationshipState(
        version=version,
        supersedes=supersedes,
        relationship_id=relationship_id,
        user_id=user_id,
        target_person_id=target_person_id,
        as_of=now,
        computed_from_interaction_ids=tuple(interaction_ids),
        window_start=min(window) if window else None,
        window_end=max(window) if window else None,
        attraction=AttractionAxes(**pick("attraction")),
        intimacy=IntimacyAxes(**pick("intimacy")),
        bond=BondAxes(**pick("bond")),
        dynamics=DynamicsAxes(**pick("dynamics")),
        health=HealthAxes(**pick("health")),
        compatibility=CompatibilityAxes(unassessed_domains=unassessed_compat),
        stage=derive_stage(axes),
        model_version=MODEL_VERSION,
    )
