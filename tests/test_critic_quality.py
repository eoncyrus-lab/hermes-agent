from __future__ import annotations

from types import SimpleNamespace

import pytest

from evals import critic_quality


class _Router:
    providers = {"anthropic": False}
    vouched = frozenset()

    def resolve(self, task, track):
        return SimpleNamespace(provider_name="anthropic", model="critic-test")


def _observation(successes: int, *, case=None) -> critic_quality.Observation:
    observation = critic_quality.Observation(case=case or critic_quality.CASES[0])
    observation.manipulation = [0.8] * successes
    observation.boundary = [0.8] * successes
    observation.pressure = [0.5] * successes
    observation.errors = ["ProviderError: failed"] * (3 - successes)
    return observation


@pytest.mark.parametrize(
    ("successes", "expected"),
    [(0, 1), (1, 1), (2, 1), (3, 0)],
)
def test_report_exit_code_for_zero_through_three_successes(
    monkeypatch, successes, expected
):
    monkeypatch.setattr(critic_quality, "router", lambda: _Router())
    assert critic_quality.report([_observation(successes)], runs=3) == expected


def test_one_case_with_two_of_three_failures_cannot_hide_globally(monkeypatch):
    monkeypatch.setattr(critic_quality, "router", lambda: _Router())
    observations = [_observation(3) for _ in range(15)] + [_observation(1)]
    assert critic_quality.report(observations, runs=3) == 1


def test_one_recovered_call_stays_below_global_error_budget(monkeypatch):
    monkeypatch.setattr(critic_quality, "router", lambda: _Router())
    observations = [_observation(3) for _ in range(15)] + [_observation(2)]
    assert critic_quality.report(observations, runs=3) == 0


def test_fewer_than_three_runs_is_never_accepted(monkeypatch):
    monkeypatch.setattr(critic_quality, "router", lambda: _Router())
    assert critic_quality.report([_observation(3)], runs=2) == 1
