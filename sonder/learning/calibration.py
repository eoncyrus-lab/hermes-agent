"""Offline, read-only calibration reporting.

This module deliberately has no dependency on runtime settings. A report may
recommend a human review, but cannot mutate a confidence ceiling or provider
configuration.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from enum import Enum
from typing import Literal

from pydantic import Field

from ..domain.schemas.primitives import Frozen
from ..domain.schemas.strategy import OutcomeLabel


class CalibrationStatus(str, Enum):
    SUFFICIENT_REAL_DATA = "SUFFICIENT_REAL_DATA"
    INSUFFICIENT_REAL_DATA = "INSUFFICIENT_REAL_DATA"


class CalibrationRecommendation(str, Enum):
    HOLD = "hold"
    TIGHTEN = "tighten"
    ELIGIBLE_FOR_MANUAL_REVIEW = "eligible_for_manual_review"


class CalibrationOrigin(str, Enum):
    REAL = "real"
    SYNTHETIC = "synthetic"


class CalibrationObservationV1(Frozen):
    """One prediction joined to its later outcome for offline analysis."""

    schema_version: Literal["calibration-observation/1"] = "calibration-observation/1"
    bucket: str = Field(min_length=1, max_length=120)
    model_version: str = Field(min_length=1, max_length=120)
    policy_version: str = Field(min_length=1, max_length=120)
    predicted_probability: float = Field(ge=0.0, le=1.0)
    outcome: OutcomeLabel
    origin: CalibrationOrigin
    valid_for_learning: bool = True


class MetricIntervalV1(Frozen):
    lower: float
    upper: float
    level: float = 0.95


class CalibrationDataQualityV1(Frozen):
    total: int = Field(ge=0)
    real_usable: int = Field(ge=0)
    synthetic: int = Field(ge=0)
    indeterminate: int = Field(ge=0)
    invalid_for_learning: int = Field(ge=0)


class CalibrationGroupReportV1(Frozen):
    bucket: str
    model_version: str
    policy_version: str
    status: CalibrationStatus
    recommendation: CalibrationRecommendation
    reason: str
    sample_size: int = Field(ge=0)
    brier: float | None = None
    bias: float | None = None
    brier_interval: MetricIntervalV1 | None = None
    bias_interval: MetricIntervalV1 | None = None
    data_quality: CalibrationDataQualityV1


class CalibrationReportV1(Frozen):
    schema_version: Literal["calibration-report/1"] = "calibration-report/1"
    status: CalibrationStatus
    recommendation: CalibrationRecommendation
    minimum_real_samples: int = Field(ge=1)
    groups: tuple[CalibrationGroupReportV1, ...]


_ACTUAL = {
    OutcomeLabel.POSITIVE: 1.0,
    OutcomeLabel.NEUTRAL: 0.5,
    OutcomeLabel.NEGATIVE: 0.0,
}


def _mean_interval(
    values: list[float], lower_bound: float, upper_bound: float
) -> MetricIntervalV1:
    """Normal interval for a bounded metric, clipped to its valid range."""
    mean = statistics.fmean(values)
    if len(values) < 2:
        return MetricIntervalV1(lower=round(mean, 4), upper=round(mean, 4))
    margin = 1.96 * statistics.stdev(values) / math.sqrt(len(values))
    return MetricIntervalV1(
        lower=round(max(lower_bound, mean - margin), 4),
        upper=round(min(upper_bound, mean + margin), 4),
    )


def _group_report(
    key: tuple[str, str, str],
    observations: list[CalibrationObservationV1],
    minimum_real_samples: int,
) -> CalibrationGroupReportV1:
    bucket, model_version, policy_version = key
    synthetic = sum(item.origin is CalibrationOrigin.SYNTHETIC for item in observations)
    indeterminate = sum(
        item.outcome is OutcomeLabel.INDETERMINATE for item in observations
    )
    invalid = sum(not item.valid_for_learning for item in observations)
    usable = [
        item
        for item in observations
        if item.origin is CalibrationOrigin.REAL
        and item.valid_for_learning
        and item.outcome in _ACTUAL
    ]
    quality = CalibrationDataQualityV1(
        total=len(observations),
        real_usable=len(usable),
        synthetic=synthetic,
        indeterminate=indeterminate,
        invalid_for_learning=invalid,
    )
    common = {
        "bucket": bucket,
        "model_version": model_version,
        "policy_version": policy_version,
        "sample_size": len(usable),
        "data_quality": quality,
    }
    if len(usable) < minimum_real_samples:
        excluded = len(observations) - len(usable)
        return CalibrationGroupReportV1(
            **common,
            status=CalibrationStatus.INSUFFICIENT_REAL_DATA,
            recommendation=CalibrationRecommendation.HOLD,
            reason=(
                f"requires {minimum_real_samples} real learnable outcomes; "
                f"found {len(usable)} ({excluded} excluded)"
            ),
        )

    errors = [item.predicted_probability - _ACTUAL[item.outcome] for item in usable]
    squared_errors = [error**2 for error in errors]
    brier = statistics.fmean(squared_errors)
    bias = statistics.fmean(errors)
    bias_interval = _mean_interval(errors, -1.0, 1.0)
    recommendation = (
        CalibrationRecommendation.TIGHTEN
        if bias_interval.lower > 0.0
        else CalibrationRecommendation.ELIGIBLE_FOR_MANUAL_REVIEW
    )
    reason = (
        "real outcomes show statistically directional overconfidence; "
        "a human may tighten the policy"
        if recommendation is CalibrationRecommendation.TIGHTEN
        else "real-data minimum met; metrics are eligible for human review"
    )
    return CalibrationGroupReportV1(
        **common,
        status=CalibrationStatus.SUFFICIENT_REAL_DATA,
        recommendation=recommendation,
        reason=reason,
        brier=round(brier, 4),
        bias=round(bias, 4),
        brier_interval=_mean_interval(squared_errors, 0.0, 1.0),
        bias_interval=bias_interval,
    )


def build_calibration_report(
    observations: list[CalibrationObservationV1], minimum_real_samples: int = 30
) -> CalibrationReportV1:
    """Group observations without ever changing a runtime policy.

    Synthetic, indeterminate, and invalid observations remain visible in data
    quality counts but never contribute to metrics or the sample-size floor.
    """
    if minimum_real_samples < 1:
        raise ValueError("minimum_real_samples must be at least 1")

    grouped: dict[tuple[str, str, str], list[CalibrationObservationV1]] = defaultdict(
        list
    )
    for item in observations:
        grouped[(item.bucket, item.model_version, item.policy_version)].append(item)
    groups = tuple(
        _group_report(key, grouped[key], minimum_real_samples) for key in sorted(grouped)
    )
    sufficient = [
        group
        for group in groups
        if group.status is CalibrationStatus.SUFFICIENT_REAL_DATA
    ]
    status = (
        CalibrationStatus.SUFFICIENT_REAL_DATA
        if sufficient
        else CalibrationStatus.INSUFFICIENT_REAL_DATA
    )
    if any(
        group.recommendation is CalibrationRecommendation.TIGHTEN for group in sufficient
    ):
        recommendation = CalibrationRecommendation.TIGHTEN
    elif sufficient:
        recommendation = CalibrationRecommendation.ELIGIBLE_FOR_MANUAL_REVIEW
    else:
        recommendation = CalibrationRecommendation.HOLD
    return CalibrationReportV1(
        status=status,
        recommendation=recommendation,
        minimum_real_samples=minimum_real_samples,
        groups=groups,
    )


class OfflineCalibrationReporter:
    """Small callable facade for batch jobs and notebooks."""

    def __init__(self, minimum_real_samples: int = 30) -> None:
        if minimum_real_samples < 1:
            raise ValueError("minimum_real_samples must be at least 1")
        self.minimum_real_samples = minimum_real_samples

    def report(self, observations: list[CalibrationObservationV1]) -> CalibrationReportV1:
        return build_calibration_report(observations, self.minimum_real_samples)


report_calibration = build_calibration_report
