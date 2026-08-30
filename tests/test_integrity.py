from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from sonder.domain.schemas.interaction import Signal, SignalType, Speaker
from sonder.domain.schemas.preference import PreferenceDomain, UserTacticPrior
from sonder.state import preference, relationship


def _signal(relationship_id, interaction_id, message_id, magnitude=0.5):
    return Signal(
        relationship_id=relationship_id,
        interaction_id=interaction_id,
        type=SignalType.WARMTH,
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
        magnitude=magnitude,
        confidence=0.7,
        by=Speaker.TARGET,
        evidence_message_ids=(message_id,),
        extractor_version="test",
    )


def test_state_input_fingerprint_is_order_independent_but_content_sensitive():
    relationship_id, interaction_id = uuid4(), uuid4()
    first = _signal(relationship_id, interaction_id, uuid4())
    second = _signal(relationship_id, interaction_id, uuid4())

    left = relationship.input_fingerprint([first, second], [interaction_id])
    right = relationship.input_fingerprint([second, first], [interaction_id])
    changed = relationship.input_fingerprint(
        [first, second.model_copy(update={"magnitude": 0.9})], [interaction_id]
    )

    assert left == right
    assert changed != left


def test_preference_fingerprint_includes_priors_and_evidence():
    user_id, evidence_id = uuid4(), uuid4()
    row = SimpleNamespace(
        id=evidence_id,
        direction="positive",
        strength=0.7,
        excluded_from_learning=False,
    )
    prior = UserTacticPrior(
        user_id=user_id,
        tactic_key="curiosity",
        domain=PreferenceDomain.COMMUNICATION,
        prior_alpha=2,
        prior_beta=1,
        n_relationships=3,
    )
    baseline = preference.input_fingerprint([row], {"curiosity": prior})
    assert baseline == preference.input_fingerprint([row], {"curiosity": prior})
    assert baseline != preference.input_fingerprint([], {"curiosity": prior})
    assert baseline != preference.input_fingerprint([row], {})


@pytest.mark.asyncio
async def test_worker_rejects_mismatched_owner_before_llm_or_write(monkeypatch):
    import sonder.worker.tasks as tasks

    claimed_user, actual_user, relationship_id = uuid4(), uuid4(), uuid4()

    @asynccontextmanager
    async def session(_scope):
        async def get(model, _ident):
            assert model.__name__ == "RelationshipRow"
            return SimpleNamespace(user_id=actual_user)

        yield SimpleNamespace(get=get, execute=AsyncMock())

    parse = AsyncMock()
    monkeypatch.setattr(tasks, "worker_session", session)
    monkeypatch.setattr(tasks.ingest_mod, "parse_screenshots", parse)

    with pytest.raises(tasks.OwnershipMismatch):
        await tasks.ingest_screenshots(
            {}, str(claimed_user), str(relationship_id), "wechat", [b"image"]
        )

    parse.assert_not_awaited()
