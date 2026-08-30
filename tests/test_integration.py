"""End-to-end through a real Postgres, with the LLM stubbed.

Proves the pipeline actually connects: interaction -> signals -> state ->
strategy -> candidates -> humanize -> critic -> ranked suggestion -> a frozen
StrategyRecord, and that RLS holds while it happens.

Skipped unless SONDER_TEST_DATABASE_URL points at a scratch database.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

DB_URL = os.environ.get("SONDER_TEST_DATABASE_URL")
pytestmark = [
    pytest.mark.skipif(not DB_URL, reason="SONDER_TEST_DATABASE_URL not set"),
    pytest.mark.asyncio,
]

NOW = datetime.now(UTC)


class StubRouter:
    """Deterministic stand-in. The point is the wiring, not the model."""

    def __init__(self):
        self.calls: list[str] = []

    def model_for(self, task):
        return "stub"

    async def complete(self, task, messages, *, system=None, **kw):
        from sonder.llm.provider import Completion

        self.calls.append(task.value)
        # Humanizer must shorten; returning the input unchanged is allowed by
        # the contract but would not exercise the length guard.
        return Completion(text="明天有空吗", model="stub", output_tokens=5)

    async def structured(self, task, messages, schema, **kw):
        self.calls.append(f"{task.value}:{schema.__name__}")
        name = schema.__name__
        if name == "_Batch":
            return schema(
                candidates=[
                    {"text": f"候选 {i}", "tactic": "curiosity", "tone": "lighter"}
                    for i in range(4)
                ]
            )
        if name == "_ModelScores":
            return schema(
                naturalness=0.8,
                voice_match=0.7,
                context_match=0.8,
                strategy_match=0.75,
                tension=0.5,
                cringe_risk=0.1,
                pressure=0.1,
                manipulation_risk=0.05,
                boundary_risk=0.05,
                note="stub",
            )
        raise AssertionError(f"unexpected schema {name}")


@pytest.fixture
async def wired(db, monkeypatch):
    stub = StubRouter()
    for mod in (
        "sonder.agent.generator",
        "sonder.agent.humanizer",
        "sonder.agent.critic",
        "sonder.agent.orchestrator",
    ):
        monkeypatch.setattr(f"{mod}.router", lambda: stub, raising=False)
    return stub


async def _seed(session, user_id, person_id, rel_id, name="T"):
    await session.execute(
        text("INSERT INTO users (id) VALUES (:u) ON CONFLICT DO NOTHING"),
        {"u": user_id},
    )
    await session.execute(
        text(
            "INSERT INTO person_profiles (id,user_id,subject,data_subject,"
            "display_name,version,retention_until) VALUES "
            "(:p,:u,'target','third_party',:name,1, now() + interval '1 year')"
        ),
        {"p": person_id, "u": user_id, "name": name},
    )
    await session.execute(
        text("INSERT INTO relationships (id,user_id,target_person_id) VALUES (:r,:u,:p)"),
        {"r": rel_id, "u": user_id, "p": person_id},
    )


async def test_full_reply_path(wired, register):
    from sonder.agent import orchestrator
    from sonder.domain.schemas.interaction import (
        Channel,
        IngestSource,
        Interaction,
        Message,
        Signal,
        SignalType,
        Speaker,
    )
    from sonder.state.relationship import compute
    from sonder.storage.db import dyad_session
    from sonder.storage.repositories import InteractionRepo, SignalRepo, StateRepo
    from sonder.storage.scope import Scope

    user_id, person_id, rel_id = uuid4(), uuid4(), uuid4()
    # Every scoped session must now present a claim backed by a live token;
    # setting app.user_id alone proves nothing to the policies.
    await register(user_id)
    scope = Scope(user_id, rel_id)

    async with dyad_session(scope) as s:
        await _seed(s, user_id, person_id, rel_id)

    # 1. an interaction lands
    async with dyad_session(scope) as s:
        iid = uuid4()
        msgs = tuple(
            Message(
                interaction_id=iid,
                sender=Speaker.TARGET if i % 2 else Speaker.SELF,
                sent_at=NOW - timedelta(minutes=30 - i),
                seq=i,
                text=f"m{i}",
                text_sha256="a" * 64,
            )
            for i in range(6)
        )
        await InteractionRepo(s).add(
            scope,
            Interaction(
                id=iid,
                relationship_id=rel_id,
                channel=Channel.WECHAT,
                source=IngestSource.MANUAL_ENTRY,
                started_at=msgs[0].sent_at,
                ended_at=msgs[-1].sent_at,
                messages=msgs,
                message_count_self=3,
                message_count_target=3,
                extractor_version="test",
            ),
        )

        # 2. signals over it
        sigs = [
            Signal(
                relationship_id=rel_id,
                interaction_id=iid,
                type=SignalType.FLIRT_RECIPROCATED,
                occurred_at=NOW - timedelta(days=d),
                magnitude=0.8,
                confidence=0.8,
                by=Speaker.TARGET,
                evidence_message_ids=(msgs[1].id,),
                extractor_version="test",
            )
            for d in (1, 3, 6, 9)
        ]
        await SignalRepo(s).add_many(scope, sigs)

        # 3. state derived from them
        state = compute(
            sigs,
            relationship_id=rel_id,
            user_id=user_id,
            target_person_id=person_id,
            version=1,
            supersedes=None,
            interaction_ids=[iid],
            now=NOW,
        )
        await StateRepo(s).append(scope, state, input_fingerprint="fp-1")

    # 4. the hot path end to end
    async with dyad_session(scope) as s:
        result = await orchestrator.reply(s, scope, user_id)

    assert result.suggestions, "no suggestion survived the critic"
    assert result.plan.objective is not None
    head = result.suggestions[0]
    assert head.strategy_id is not None, "the prediction must have been recorded"

    # 5. the prediction is on disk, frozen, and predates any outcome
    async with dyad_session(scope) as s:
        row = (
            await s.execute(
                text(
                    "SELECT predicted_positive, prediction_basis, user_action "
                    "FROM strategy_records WHERE id = :i"
                ),
                {"i": head.strategy_id},
            )
        ).first()
    assert row is not None
    assert 0.0 <= row[0] <= 1.0
    assert row[1].strip(), "prediction_basis must be populated"
    assert row[2] == "not_yet"

    stub = wired
    assert any(c.startswith("generate") for c in stub.calls)
    assert "humanize" in stub.calls
    assert any("_ModelScores" in c for c in stub.calls), "critic must have run"


async def test_rls_blocks_the_other_relationship(wired, register):
    """Two relationships, one user. Reading across must return nothing."""
    from sonder.storage.db import dyad_session
    from sonder.storage.scope import Scope

    user_id = uuid4()
    p1, p2, r1, r2 = uuid4(), uuid4(), uuid4(), uuid4()
    await register(user_id)

    async with dyad_session(Scope(user_id, r1)) as s:
        await _seed(s, user_id, p1, r1)
    async with dyad_session(Scope(user_id, r2)) as s:
        await s.execute(
            text(
                "INSERT INTO person_profiles (id,user_id,subject,data_subject,"
                "display_name,version,retention_until) VALUES "
                "(:p,:u,'target','third_party','U',1, now() + interval '1 year')"
            ),
            {"p": p2, "u": user_id},
        )
        await s.execute(
            text(
                "INSERT INTO relationships (id,user_id,target_person_id) "
                "VALUES (:r,:u,:p)"
            ),
            {"r": r2, "u": user_id, "p": p2},
        )
        await s.execute(
            text(
                "INSERT INTO memory_chunks (relationship_id,kind,summary) "
                "VALUES (:r,'inside_joke','the penguin thing')"
            ),
            {"r": r2},
        )

    # scoped to r1, a deliberately unfiltered query still cannot see r2's row
    async with dyad_session(Scope(user_id, r1)) as s:
        leaked = (await s.execute(text("SELECT count(*) FROM memory_chunks"))).scalar()
    assert leaked == 0, "RLS did not isolate the other relationship"


async def test_service_roles_have_expected_rls_privileges(db, register):
    """The request DSN must not be the schema owner or a bypass role.

    And of the six service identities, exactly one may hold BYPASSRLS. That
    one is reserved for scheduled cross-user jobs in the worker: no request
    path, and no queued job, is allowed to carry it.
    """
    from sonder.storage.db import dyad_session
    from sonder.storage.scope import Scope

    user_id = uuid4()
    await register(user_id)
    async with dyad_session(Scope(user_id, uuid4())) as s:
        current = (await s.execute(text("SELECT current_user"))).scalar_one()
        roles = {
            name: (superuser, bypass_rls)
            for name, superuser, bypass_rls in (
                await s.execute(
                    text(
                        "SELECT rolname, rolsuper, rolbypassrls FROM pg_roles "
                        "WHERE rolname LIKE 'sonder\\_%'"
                    )
                )
            ).all()
        }

    assert current == "sonder_app"
    assert roles == {
        "sonder_app": (False, False),
        "sonder_bot": (False, False),
        "sonder_erase": (False, False),
        "sonder_identity": (False, False),
        "sonder_worker": (False, False),
        "sonder_maint": (False, True),
    }
    assert [n for n, (_, bypass) in roles.items() if bypass] == ["sonder_maint"]


async def test_spoofed_relationship_cannot_call_cross_dyad_oracle(db, register):
    """A caller-set victim relationship must not unlock cross-dyad text."""
    from sonder.storage.db import dyad_session, maintenance_session
    from sonder.storage.scope import Scope

    victim, attacker = uuid4(), uuid4()
    p1, p2, p3 = uuid4(), uuid4(), uuid4()
    r1, r2, r3 = uuid4(), uuid4(), uuid4()

    async with maintenance_session() as s:
        await _seed(s, victim, p1, r1, "V1")
        await _seed(s, victim, p2, r2, "V2")
        await _seed(s, attacker, p3, r3, "A1")
        await s.execute(
            text(
                "INSERT INTO memory_chunks (relationship_id,kind,summary) VALUES "
                "(:r2,'callback','victim secret'),"
                "(:r3,'nickname','attacker phrase')"
            ),
            {"r2": r2, "r3": r3},
        )

    await register(attacker)
    # Simulate the reviewed attack: a shared app role sets the victim's GUC.
    # The former SECURITY DEFINER oracle no longer exists or is executable.
    with pytest.raises(DBAPIError):
        async with dyad_session(Scope(attacker, r1)) as s:
            await s.execute(
                text("SELECT * FROM other_dyad_phrases_for_current_relationship()")
            )


async def test_relationship_cannot_reference_another_users_profile(db, register):
    """DB ownership consistency prevents cross-account deletion cascades."""
    from sonder.storage.db import dyad_session, maintenance_session
    from sonder.storage.deletion import forget_person, inspect_person
    from sonder.storage.scope import Scope

    user_a, user_b = uuid4(), uuid4()
    person_a, person_b = uuid4(), uuid4()
    relationship_a, relationship_b = uuid4(), uuid4()

    async with maintenance_session() as s:
        await _seed(s, user_a, person_a, relationship_a)
        await _seed(s, user_b, person_b, relationship_b)

    await register(user_b)
    with pytest.raises(IntegrityError):
        async with dyad_session(Scope(user_b, uuid4())) as s:
            await s.execute(
                text(
                    "INSERT INTO relationships (user_id,target_person_id) "
                    "VALUES (:user,:person)"
                ),
                {"user": user_b, "person": person_a},
            )

    async with maintenance_session() as s:
        await forget_person(s, person_a)
    async with maintenance_session() as s:
        retained = await inspect_person(s, person_b)

    assert retained["found"]
    assert retained["relationships"] == 1


async def test_derived_rows_cannot_mix_relationship_ownership(db):
    """Every duplicated relationship key is protected by a composite FK."""
    from sonder.storage.db import maintenance_session

    u1, u2 = uuid4(), uuid4()
    p1, p2, r1, r2 = uuid4(), uuid4(), uuid4(), uuid4()
    i1, i2, state1, state2, strategy1, strategy2 = (uuid4() for _ in range(6))

    async with maintenance_session() as s:
        await _seed(s, u1, p1, r1, "D1")
        await _seed(s, u2, p2, r2, "D2")
        await s.execute(
            text(
                "INSERT INTO interactions (id,relationship_id,channel,source,"
                "started_at,extractor_version) VALUES "
                "(:i1,:r1,'wechat','manual_entry',now(),'t'),"
                "(:i2,:r2,'wechat','manual_entry',now(),'t')"
            ),
            {"i1": i1, "r1": r1, "i2": i2, "r2": r2},
        )
        await s.execute(
            text(
                "INSERT INTO relationship_states "
                "(id,relationship_id,version,as_of,stage,model_version,"
                "input_fingerprint) VALUES "
                "(:s1,:r1,1,now(),'s1','t','fp1'),"
                "(:s2,:r2,1,now(),'s1','t','fp2')"
            ),
            {"s1": state1, "r1": r1, "s2": state2, "r2": r2},
        )
        await s.execute(
            text(
                "INSERT INTO strategy_records "
                "(id,relationship_id,user_id,objective,tactic_keys,"
                "relationship_state_id,rendered_text_sha256,predicted_positive,"
                "prediction_basis,model_version) VALUES "
                "(:x1,:r1,:u1,'reply',ARRAY['t'],:s1,'sha',0.5,'b','t'),"
                "(:x2,:r2,:u2,'reply',ARRAY['t'],:s2,'sha',0.5,'b','t')"
            ),
            {
                "x1": strategy1,
                "r1": r1,
                "u1": u1,
                "s1": state1,
                "x2": strategy2,
                "r2": r2,
                "u2": u2,
                "s2": state2,
            },
        )

    rejected = (
        (
            "INSERT INTO signals (relationship_id,interaction_id,type,occurred_at,"
            "magnitude,confidence,by_speaker,evidence_message_ids,extractor_version) "
            "VALUES (:r1,:i2,'warmth',now(),0.5,0.5,'target',ARRAY[:e]::uuid[],'t')",
            {"r1": r1, "i2": i2, "e": uuid4()},
        ),
        (
            "INSERT INTO preference_evidence (person_id,relationship_id,domain,key,"
            "direction,provenance,observed_at) VALUES "
            "(:p2,:r1,'flirt_mode','x','positive','user_stated',now())",
            {"p2": p2, "r1": r1},
        ),
        (
            "INSERT INTO preference_models (person_id,relationship_id,version,"
            "scorer_version) VALUES (:p2,:r1,1,'t')",
            {"p2": p2, "r1": r1},
        ),
        (
            "INSERT INTO strategy_records (relationship_id,user_id,objective,"
            "tactic_keys,relationship_state_id,rendered_text_sha256,"
            "predicted_positive,prediction_basis,model_version) VALUES "
            "(:r1,:u2,'reply',ARRAY['t'],:s1,'sha',0.5,'b','t')",
            {"r1": r1, "u2": u2, "s1": state1},
        ),
        (
            "INSERT INTO strategy_records (relationship_id,user_id,objective,"
            "tactic_keys,relationship_state_id,rendered_text_sha256,"
            "predicted_positive,prediction_basis,model_version) VALUES "
            "(:r1,:u1,'reply',ARRAY['t'],:s2,'sha',0.5,'b','t')",
            {"r1": r1, "u1": u1, "s2": state2},
        ),
        (
            "INSERT INTO strategy_outcomes (strategy_id,relationship_id,observed,"
            "label,window_start,window_end,scorer_version) VALUES "
            "(:x2,:r1,'{}','neutral',now(),now(),'t')",
            {"x2": strategy2, "r1": r1},
        ),
    )

    for statement, params in rejected:
        with pytest.raises(IntegrityError):
            async with maintenance_session() as s:
                await s.execute(text(statement), params)
