"""Outcome scoring and the learning loop (doc s41-43).

Runs after the observation window closes, never in the request path. The
separation is what makes the prediction honest: by the time this code runs,
StrategyRecord.predicted_positive was written and frozen hours or days ago.

The scoring is deliberately multi-signal (doc s42). "She replied fast" is the
metric that makes a system optimise for pressure, because pressure does
produce fast replies.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import structlog

from ..domain.schemas.interaction import SignalType, Speaker
from ..domain.schemas.preference import (
    Direction,
    PreferenceDomain,
    PreferenceEvidence,
)
from ..domain.schemas.primitives import Provenance
from ..domain.schemas.strategy import ObservedOutcome, OutcomeLabel, StrategyOutcome

log = structlog.get_logger(__name__)
SCORER_VERSION = "outcome/1.0.0"

POSITIVE_SIGNALS = {
    SignalType.ENGAGEMENT_UP,
    SignalType.RECIPROCATION,
    SignalType.SELF_DISCLOSURE,
    SignalType.TEASING_RETURNED,
    SignalType.FLIRT_RECIPROCATED,
    SignalType.PLAN_ACCEPTED,
    SignalType.INITIATIVE,
    SignalType.WARMTH,
}
NEGATIVE_SIGNALS = {
    SignalType.ENGAGEMENT_DOWN,
    SignalType.AVOIDANCE,
    SignalType.PLAN_DEFLECTED,
}
# These do not merely count against the outcome; they decide it.
DOMINANT_NEGATIVE = {SignalType.DISCOMFORT, SignalType.BOUNDARY_SET}


@dataclass(slots=True)
class Window:
    start: datetime
    end: datetime
    signals: list
    messages_before: list
    messages_after: list


def observe(window: Window) -> ObservedOutcome:
    target_signals = [s for s in window.signals if s.by_speaker == Speaker.TARGET.value]

    def any_of(types: set[SignalType]) -> bool:
        return any(s.type in {t.value for t in types} for s in target_signals)

    before = [m for m in window.messages_before if m.sender == Speaker.TARGET.value]
    after = [m for m in window.messages_after if m.sender == Speaker.TARGET.value]

    def med(rows, attr: str) -> float | None:
        vals = [
            getattr(r, attr, None) or (r.features or {}).get(attr)
            for r in rows
            if getattr(r, attr, None) or (r.features or {}).get(attr)
        ]
        return statistics.median(vals) if vals else None

    lat_before, lat_after = med(before, "reply_latency_s"), med(after, "reply_latency_s")
    len_before, len_after = med(before, "char_len"), med(after, "char_len")

    def delta(b, a) -> float | None:
        if b is None or a is None or b == 0:
            return None
        return round(max(-1.0, min(1.0, (a - b) / b)), 3)

    lat_delta = delta(lat_before, lat_after)
    return ObservedOutcome(
        reply_latency_delta=lat_delta,
        reply_length_delta=delta(len_before, len_after),
        question_returned=any((m.features or {}).get("is_question") for m in after)
        or None,
        topic_continued=bool(after) or None,
        flirt_reciprocated=any_of({SignalType.FLIRT_RECIPROCATED}) or None,
        disclosure_deepened=any_of({SignalType.SELF_DISCLOSURE}) or None,
        initiated_next_contact=any_of({SignalType.INITIATIVE}) or None,
        plan_accepted=any_of({SignalType.PLAN_ACCEPTED}) or None,
        discomfort_or_boundary=any_of(DOMINANT_NEGATIVE) or None,
    )


def label(observed: ObservedOutcome) -> tuple[OutcomeLabel, bool]:
    """Returns (label, valid_for_learning)."""
    if observed.discomfort_or_boundary:
        return OutcomeLabel.NEGATIVE, True

    positives = sum(
        1
        for f in (
            observed.question_returned,
            observed.flirt_reciprocated,
            observed.disclosure_deepened,
            observed.initiated_next_contact,
            observed.plan_accepted,
        )
        if f
    )
    for d in (observed.reply_length_delta,):
        if d is not None and d > 0.25:
            positives += 1

    negatives = 0
    if observed.topic_continued is None or observed.topic_continued is False:
        negatives += 1
    for d in (observed.reply_length_delta,):
        if d is not None and d < -0.35:
            negatives += 1

    observed_anything = any(v is not None for v in observed.model_dump().values())
    if not observed_anything:
        return OutcomeLabel.INDETERMINATE, False
    if positives == 0 and negatives == 0:
        return OutcomeLabel.NEUTRAL, True
    if positives > negatives:
        return OutcomeLabel.POSITIVE, True
    if negatives > positives:
        return OutcomeLabel.NEGATIVE, True
    return OutcomeLabel.NEUTRAL, True


def score(
    strategy_row,
    window: Window,
    confounders: tuple[str, ...] = (),
) -> StrategyOutcome:
    observed = observe(window)
    lbl, valid = label(observed)

    # A message the user rewrote does not test the model's tactic; keeping it
    # in the training set teaches the model from someone else's writing.
    if strategy_row.user_action == "sent_edited":
        valid = False
        confounders = (*confounders, "user edited the suggestion before sending")
    if strategy_row.user_action == "discarded":
        valid = False

    return StrategyOutcome(
        strategy_id=strategy_row.id,
        relationship_id=strategy_row.relationship_id,
        observed=observed,
        label=lbl,
        window_start=window.start,
        window_end=window.end,
        confounders=confounders,
        valid_for_learning=valid,
        scorer_version=SCORER_VERSION,
    )


def to_preference_evidence(
    strategy_row, outcome: StrategyOutcome, person_id: UUID, stage: str | None
) -> list[PreferenceEvidence]:
    """Turn a scored outcome into evidence about this person's preferences.

    Only when the outcome is attributable: a confounded window teaches the
    model about the confounder, not about her.
    """
    if not outcome.attributable:
        return []
    direction = {
        OutcomeLabel.POSITIVE: Direction.POSITIVE,
        OutcomeLabel.NEGATIVE: Direction.NEGATIVE,
        OutcomeLabel.NEUTRAL: Direction.NEUTRAL,
    }.get(outcome.label)
    if direction is None:
        return []

    return [
        PreferenceEvidence(
            person_id=person_id,
            relationship_id=strategy_row.relationship_id,
            domain=PreferenceDomain.FLIRT_MODE,
            key=key,
            direction=direction,
            strength=0.6,
            provenance=Provenance.OBSERVED_BEHAVIOR,
            observed_at=outcome.window_end,
            source_strategy_outcome_id=outcome.id,
            stage_at_time=stage,
            known_confounders=tuple(outcome.confounders),
        )
        for key in strategy_row.tactic_keys
    ]


def calibration(pairs: list[tuple[float, OutcomeLabel]]) -> dict[str, float]:
    """Brier score and bias over scored predictions (doc s58)."""
    actual_map = {
        OutcomeLabel.POSITIVE: 1.0,
        OutcomeLabel.NEUTRAL: 0.5,
        OutcomeLabel.NEGATIVE: 0.0,
    }
    usable = [(p, actual_map[lbl]) for p, lbl in pairs if lbl in actual_map]
    if not usable:
        return {"n": 0}
    brier = sum((p - a) ** 2 for p, a in usable) / len(usable)
    bias = sum(p - a for p, a in usable) / len(usable)
    return {
        "n": len(usable),
        "brier": round(brier, 4),
        "bias": round(bias, 4),
        "mean_predicted": round(sum(p for p, _ in usable) / len(usable), 3),
        "mean_actual": round(sum(a for _, a in usable) / len(usable), 3),
    }


def recompute_priors(
    per_relationship: dict[UUID, dict[str, tuple[int, int]]], min_k: int = 3
) -> dict[str, tuple[float, float]]:
    """User-level tactic priors from per-relationship counts (doc s43).

    Each relationship contributes its RATE, not its raw counts, so one
    intensively-logged person cannot dominate the user's prior.
    """
    by_key: dict[str, list[float]] = {}
    for counts in per_relationship.values():
        for key, (pos, total) in counts.items():
            if total >= 3:
                by_key.setdefault(key, []).append(pos / total)

    out: dict[str, tuple[float, float]] = {}
    for key, rates in by_key.items():
        if len(rates) < min_k:
            continue
        mean = statistics.mean(rates)
        strength = 2.0  # deliberately weak; person-level evidence must win fast
        out[key] = (
            round(max(0.1, mean * strength), 3),
            round(max(0.1, (1 - mean) * strength), 3),
        )
    return out
