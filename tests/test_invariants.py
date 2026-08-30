"""The invariants. If any of these break, the product is doing the thing it
exists not to do."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from sonder.domain.schemas import (
    Alternative,
    Assessment,
    BetaEstimate,
    ConsentRecord,
    ConsentStatus,
    DataSubject,
    Direction,
    EvidenceKind,
    EvidenceRef,
    PersonProfile,
    PreferenceDomain,
    PreferenceEvidence,
    PreferenceModel,
    Provenance,
    Subject,
)
from sonder.domain.schemas.interaction import Signal, SignalType, Speaker
from sonder.domain.schemas.preference import UserTacticPrior
from sonder.domain.schemas.primitives import CONFIDENCE_CEILING_POLICY
from sonder.domain.schemas.strategy import (
    CriticScores,
    Objective,
    ObservedOutcome,
    OutcomeLabel,
    StrategyOutcome,
    StrategyRecord,
    UserAction,
)
from sonder.state.relationship import compute

NOW = datetime.now(UTC)


def ev(n: int = 1) -> tuple[EvidenceRef, ...]:
    return tuple(
        EvidenceRef(kind=EvidenceKind.MESSAGE, ref_id=uuid4(), occurred_at=NOW)
        for _ in range(n)
    )


def alt() -> tuple[Alternative, ...]:
    return (Alternative(hypothesis="ordinary politeness", plausibility=0.5),)


# -------------------------------------------------------------- 1. evidence


def test_inference_requires_evidence():
    with pytest.raises(ValidationError, match="at least one EvidenceRef"):
        Assessment[float](
            value=0.9, confidence=0.3, provenance=Provenance.MODEL_INFERENCE
        )


def test_user_statement_needs_no_evidence():
    a = Assessment[str](
        value="works in finance", confidence=0.95, provenance=Provenance.USER_STATED
    )
    assert a.is_actionable


def test_assessment_rejects_duplicate_evidence_identity():
    ref_id = uuid4()
    original = EvidenceRef(
        kind=EvidenceKind.MESSAGE, ref_id=ref_id, occurred_at=NOW, note="original"
    )
    copied = EvidenceRef(
        kind=EvidenceKind.MESSAGE,
        ref_id=ref_id,
        occurred_at=NOW + timedelta(seconds=1),
        note="copied metadata",
    )
    with pytest.raises(ValidationError, match="duplicate evidence identity"):
        Assessment[float](
            value=0.5,
            confidence=0.4,
            provenance=Provenance.MODEL_INFERENCE,
            evidence=(original, copied),
            alternatives=alt(),
        )


# ------------------------------------------------------------ 2. confidence


@pytest.mark.parametrize("n,too_high", [(1, 0.5), (2, 0.7), (4, 0.8), (8, 0.9)])
def test_confidence_is_capped_by_evidence_count(n, too_high):
    with pytest.raises(ValidationError, match="exceeds ceiling"):
        Assessment[float](
            value=0.8,
            confidence=too_high,
            provenance=Provenance.MODEL_INFERENCE,
            evidence=ev(n),
        )


def test_low_confidence_requires_an_alternative():
    with pytest.raises(ValidationError, match="alternative hypothesis"):
        Assessment[float](
            value=0.5,
            confidence=0.4,
            provenance=Provenance.MODEL_INFERENCE,
            evidence=ev(2),
        )


def test_confidence_ceiling_policy_is_versioned():
    assert CONFIDENCE_CEILING_POLICY.version == "confidence-ceiling/1.0.0"
    assert CONFIDENCE_CEILING_POLICY.thresholds[0] == (0, 0.0)


def test_relationship_state_counts_a_signal_id_once():
    relationship_id, interaction_id = uuid4(), uuid4()
    signal = Signal(
        relationship_id=relationship_id,
        interaction_id=interaction_id,
        type=SignalType.FLIRT_RECIPROCATED,
        occurred_at=NOW,
        magnitude=0.8,
        confidence=0.8,
        by=Speaker.TARGET,
        evidence_message_ids=(uuid4(),),
        extractor_version="test",
    )
    kwargs = {
        "relationship_id": relationship_id,
        "user_id": uuid4(),
        "target_person_id": uuid4(),
        "version": 1,
        "supersedes": None,
        "interaction_ids": [interaction_id],
        "now": NOW,
    }
    once = compute([signal], **kwargs)
    duplicated = compute([signal, signal], **kwargs)
    for first, second in (
        (once.attraction.romantic, duplicated.attraction.romantic),
        (once.attraction.sexual, duplicated.attraction.sexual),
    ):
        assert first is not None and second is not None
        assert second.value == first.value
        assert second.confidence == first.confidence
        assert second.evidence == first.evidence


def test_relationship_state_counts_same_evidence_with_new_signal_id_once():
    relationship_id, interaction_id, message_id = uuid4(), uuid4(), uuid4()
    signal = Signal(
        relationship_id=relationship_id,
        interaction_id=interaction_id,
        type=SignalType.FLIRT_RECIPROCATED,
        occurred_at=NOW,
        magnitude=0.8,
        confidence=0.8,
        by=Speaker.TARGET,
        evidence_message_ids=(message_id,),
        extractor_version="test",
    )
    duplicate_row = signal.model_copy(update={"id": uuid4()})
    kwargs = {
        "relationship_id": relationship_id,
        "user_id": uuid4(),
        "target_person_id": uuid4(),
        "version": 1,
        "supersedes": None,
        "interaction_ids": [interaction_id],
        "now": NOW,
    }
    once = compute([signal], **kwargs)
    replayed = compute([signal, duplicate_row], **kwargs)
    for first, second in (
        (once.attraction.romantic, replayed.attraction.romantic),
        (once.attraction.sexual, replayed.attraction.sexual),
    ):
        assert first is not None and second is not None
        assert second.value == first.value
        assert second.confidence == first.confidence
        assert second.evidence == first.evidence


# ------------------------------------------------------------------ 3. beta


def test_thin_evidence_reports_unknown():
    b = BetaEstimate(positive=2, negative=0)
    assert b.status.value == "unknown"
    assert b.describe() == "unknown (n=2)"


def test_established_belief_reports_an_interval():
    b = BetaEstimate(positive=7, negative=1, neutral=1)
    assert b.status.value == "established"
    lo, hi = b.interval()
    assert lo < b.mean < hi


def test_prior_cannot_manufacture_knowledge():
    """A user-level prior may shift the mean. It may not create certainty."""
    weak = BetaEstimate(positive=1, negative=0, prior_alpha=3.0, prior_beta=1.0)
    assert weak.status.value == "unknown"


# ------------------------------------------------------- 4. protected fields


def test_third_party_sexual_traits_cannot_be_inferred():
    with pytest.raises(ValidationError, match="protected namespace"):
        PersonProfile(
            version=1,
            user_id=uuid4(),
            subject=Subject.TARGET,
            data_subject=DataSubject.THIRD_PARTY,
            display_name="A",
            retention_until=NOW + timedelta(days=365),
            attributes={
                "kink.submissive": Assessment[float](
                    value=0.7,
                    confidence=0.6,
                    provenance=Provenance.MODEL_INFERENCE,
                    evidence=ev(4),
                    alternatives=alt(),
                )
            },
        )


def test_protected_preference_requires_a_statement():
    with pytest.raises(ValidationError, match="explicit statement"):
        PreferenceEvidence(
            person_id=uuid4(),
            relationship_id=uuid4(),
            domain=PreferenceDomain.SEXUAL,
            key="anything",
            direction=Direction.POSITIVE,
            provenance=Provenance.OBSERVED_BEHAVIOR,
            observed_at=NOW,
            source_interaction_id=uuid4(),
        )


# ----------------------------------------------------------------- 5. consent


def test_consent_cannot_be_inferred_from_behaviour():
    with pytest.raises(ValidationError, match="explicit statement"):
        ConsentRecord(
            relationship_id=uuid4(),
            activity="staying over",
            status=ConsentStatus.CONFIRMED,
            provenance=Provenance.OBSERVED_BEHAVIOR,
            stated_at=NOW,
            stated_by="target",
            verbatim_ref=ev(1)[0],
        )


def test_consent_has_no_probabilistic_state():
    assert {s.value for s in ConsentStatus} == {
        "confirmed",
        "declined",
        "hard_limit",
        "unknown",
    }


# ------------------------------------------------------ 6. prediction first


def _record(**kw) -> StrategyRecord:
    base = dict(
        relationship_id=uuid4(),
        user_id=uuid4(),
        objective=Objective.BUILD_TENSION,
        tactic_keys=("role_contrast",),
        relationship_state_id=uuid4(),
        rendered_text_sha256="a" * 64,
        predicted_positive=0.65,
        prediction_basis="7/9 positive for her",
        model_version="v0",
    )
    base.update(kw)
    return StrategyRecord(**base)


def test_prediction_basis_is_mandatory():
    with pytest.raises(ValidationError):
        _record(prediction_basis="   ")


def test_strategy_record_is_frozen():
    rec = _record()
    with pytest.raises(ValidationError):
        rec.predicted_positive = 0.99


def test_brier_only_scores_learnable_outcomes():
    rec = _record(user_action=UserAction.SENT_VERBATIM, sent_at=NOW)
    good = StrategyOutcome(
        strategy_id=rec.id,
        relationship_id=rec.relationship_id,
        observed=ObservedOutcome(flirt_reciprocated=True),
        label=OutcomeLabel.POSITIVE,
        window_start=NOW,
        window_end=NOW + timedelta(hours=72),
        scorer_version="v0",
    )
    assert good.brier(rec) == pytest.approx(0.1225)

    contaminated = StrategyOutcome(
        strategy_id=rec.id,
        relationship_id=rec.relationship_id,
        observed=ObservedOutcome(flirt_reciprocated=True),
        label=OutcomeLabel.POSITIVE,
        window_start=NOW,
        window_end=NOW + timedelta(hours=72),
        valid_for_learning=False,
        scorer_version="v0",
    )
    assert contaminated.brier(rec) is None


# ------------------------------------------------------------- 7. discomfort


def test_boundary_signal_outranks_engagement():
    rec = _record()
    with pytest.raises(ValidationError, match="forces a NEGATIVE label"):
        StrategyOutcome(
            strategy_id=rec.id,
            relationship_id=rec.relationship_id,
            observed=ObservedOutcome(
                reply_latency_delta=-0.9,
                reply_length_delta=0.9,
                flirt_reciprocated=True,
                discomfort_or_boundary=True,
            ),
            label=OutcomeLabel.POSITIVE,
            window_start=NOW,
            window_end=NOW + timedelta(hours=1),
            scorer_version="v0",
        )


def test_indeterminate_outcomes_never_train():
    rec = _record()
    with pytest.raises(ValidationError, match="cannot train"):
        StrategyOutcome(
            strategy_id=rec.id,
            relationship_id=rec.relationship_id,
            observed=ObservedOutcome(),
            label=OutcomeLabel.INDETERMINATE,
            window_start=NOW,
            window_end=NOW + timedelta(hours=1),
            valid_for_learning=True,
            scorer_version="v0",
        )


# -------------------------------------------------------------- 8. isolation


def _pref_ev(rid, key="role_contrast", direction=Direction.POSITIVE, pid=None):
    return PreferenceEvidence(
        person_id=pid or uuid4(),
        relationship_id=rid,
        domain=PreferenceDomain.FLIRT_MODE,
        key=key,
        direction=direction,
        provenance=Provenance.OBSERVED_BEHAVIOR,
        observed_at=NOW,
        source_interaction_id=uuid4(),
    )


def test_preference_model_rejects_foreign_evidence():
    pid, rid_a, rid_b = uuid4(), uuid4(), uuid4()
    with pytest.raises(ValueError, match="another relationship"):
        PreferenceModel.build(
            person_id=pid,
            evidence=[_pref_ev(rid_a, pid=pid), _pref_ev(rid_b, pid=pid)],
            scorer_version="v0",
            relationship_id=rid_a,
        )


def test_protected_domains_never_form_cross_person_priors():
    with pytest.raises(ValidationError, match="never forms a cross-person prior"):
        UserTacticPrior(
            user_id=uuid4(),
            tactic_key="whatever",
            domain=PreferenceDomain.KINK,
            prior_alpha=2.0,
            prior_beta=2.0,
            n_relationships=20,
        )


def test_priors_require_three_relationships():
    with pytest.raises(ValidationError):
        UserTacticPrior(
            user_id=uuid4(),
            tactic_key="teasing",
            domain=PreferenceDomain.FLIRT_MODE,
            prior_alpha=2.0,
            prior_beta=1.0,
            n_relationships=2,
        )


def test_cross_dyad_leak_hard_rejects():
    scores = CriticScores(
        naturalness=0.95,
        voice_match=0.95,
        context_match=0.95,
        strategy_match=0.95,
        cringe_risk=0.0,
        ai_pattern_risk=0.0,
        pressure=0.0,
        manipulation_risk=0.0,
        boundary_risk=0.0,
        cross_dyad_leak_risk=0.06,
    )
    assert scores.hard_rejected


# ----------------------------------------------------------- 9. third party


def test_third_party_profiles_must_expire():
    with pytest.raises(ValidationError):
        PersonProfile(
            version=1,
            user_id=uuid4(),
            subject=Subject.TARGET,
            data_subject=DataSubject.THIRD_PARTY,
            display_name="A",
        )
