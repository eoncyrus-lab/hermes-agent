from __future__ import annotations

from sonder.domain.schemas.strategy import OutcomeLabel
from sonder.learning.calibration import (
    CalibrationObservationV1,
    CalibrationOrigin,
    CalibrationRecommendation,
    CalibrationStatus,
    build_calibration_report,
)
from sonder.learning.outcomes import calibration


def observation(
    *,
    predicted: float = 0.8,
    outcome: OutcomeLabel = OutcomeLabel.POSITIVE,
    synthetic: bool = False,
    valid: bool = True,
) -> CalibrationObservationV1:
    return CalibrationObservationV1(
        bucket="generate:intimate",
        model_version="model/1",
        policy_version="confidence-ceiling/1.0.0",
        predicted_probability=predicted,
        outcome=outcome,
        origin=(CalibrationOrigin.SYNTHETIC if synthetic else CalibrationOrigin.REAL),
        valid_for_learning=valid,
    )


def test_empty_report_is_insufficient_and_holds():
    report = build_calibration_report([])
    assert report.status is CalibrationStatus.INSUFFICIENT_REAL_DATA
    assert report.recommendation is CalibrationRecommendation.HOLD
    assert report.groups == ()


def test_synthetic_indeterminate_and_invalid_data_do_not_count():
    report = build_calibration_report(
        [
            observation(synthetic=True),
            observation(outcome=OutcomeLabel.INDETERMINATE),
            observation(valid=False),
        ],
        minimum_real_samples=1,
    )
    group = report.groups[0]
    assert group.status is CalibrationStatus.INSUFFICIENT_REAL_DATA
    assert group.recommendation is CalibrationRecommendation.HOLD
    assert group.sample_size == 0
    assert group.data_quality.synthetic == 1
    assert group.data_quality.indeterminate == 1
    assert group.data_quality.invalid_for_learning == 1


def test_report_groups_by_bucket_model_and_policy():
    rows = [observation(predicted=0.9, outcome=OutcomeLabel.NEGATIVE) for _ in range(4)]
    rows.append(
        CalibrationObservationV1(
            bucket="generate:standard",
            model_version="model/2",
            policy_version="confidence-ceiling/2.0.0",
            predicted_probability=0.5,
            outcome=OutcomeLabel.NEUTRAL,
            origin=CalibrationOrigin.REAL,
        )
    )
    report = build_calibration_report(rows, minimum_real_samples=2)
    assert len(report.groups) == 2
    overconfident = next(group for group in report.groups if group.sample_size == 4)
    assert overconfident.brier == 0.81
    assert overconfident.bias == 0.9
    assert overconfident.bias_interval is not None
    assert overconfident.recommendation is CalibrationRecommendation.TIGHTEN
    thin = next(group for group in report.groups if group.sample_size == 1)
    assert thin.status is CalibrationStatus.INSUFFICIENT_REAL_DATA
    assert thin.recommendation is CalibrationRecommendation.HOLD


def test_legacy_calibration_contract_remains_compatible():
    assert calibration([(0.75, OutcomeLabel.POSITIVE)]) == {
        "n": 1,
        "brier": 0.0625,
        "bias": -0.25,
        "mean_predicted": 0.75,
        "mean_actual": 1.0,
    }


def test_observation_origin_is_required_and_unknown_values_fail_closed():
    import pytest
    from pydantic import ValidationError

    values = {
        "bucket": "generate:standard",
        "model_version": "model/1",
        "policy_version": "policy/1",
        "predicted_probability": 0.5,
        "outcome": OutcomeLabel.NEUTRAL,
    }
    with pytest.raises(ValidationError, match="origin"):
        CalibrationObservationV1(**values)
    with pytest.raises(ValidationError, match="origin"):
        CalibrationObservationV1(**values, origin="unknown")
