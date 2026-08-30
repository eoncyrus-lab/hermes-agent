"""Erasure and ownership.

Deletion is the control that makes modelling a real person acceptable in this
deployment, so these tests treat it as a security boundary rather than as a
convenience: incomplete erasure and cross-account access both fail here.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text

DB_URL = os.environ.get("SONDER_TEST_DATABASE_URL")
pytestmark = [
    pytest.mark.skipif(not DB_URL, reason="SONDER_TEST_DATABASE_URL not set"),
    pytest.mark.asyncio,
]

NOW = datetime.now(UTC)


async def seed_person(session, user_id, person_id, rel_id, name="Subject"):
    await session.execute(
        text("INSERT INTO users (id) VALUES (:u) ON CONFLICT DO NOTHING"),
        {"u": user_id},
    )
    await session.execute(
        text(
            "INSERT INTO person_profiles (id,user_id,subject,data_subject,"
            "display_name,version,retention_until,traits) VALUES "
            "(:p,:u,'target','third_party',:n,1,:r,:t)"
        ),
        {
            "p": person_id,
            "u": user_id,
            "n": name,
            "r": NOW + timedelta(days=365),
            "t": '{"assertiveness": {"value": 0.8, "confidence": 0.7}}',
        },
    )
    await session.execute(
        text("INSERT INTO relationships (id,user_id,target_person_id) VALUES (:r,:u,:p)"),
        {"r": rel_id, "u": user_id, "p": person_id},
    )
    iid, sid = uuid4(), uuid4()
    await session.execute(
        text(
            "INSERT INTO interactions (id,relationship_id,channel,source,"
            "started_at,extractor_version) VALUES (:i,:r,'wechat','manual_entry',"
            "now(),'t')"
        ),
        {"i": iid, "r": rel_id},
    )
    await session.execute(
        text(
            "INSERT INTO messages (interaction_id,sender,sent_at,seq,body,"
            "text_sha256) VALUES (:i,'target',now(),0,'something private','abc')"
        ),
        {"i": iid},
    )
    await session.execute(
        text(
            "INSERT INTO relationship_states (id,relationship_id,version,as_of,"
            "stage,model_version,input_fingerprint) VALUES "
            "(:s,:r,1,now(),'s2','v0','seed-fp')"
        ),
        {"s": sid, "r": rel_id},
    )
    await session.execute(
        text(
            "INSERT INTO strategy_records (id,relationship_id,user_id,objective,"
            "tactic_keys,relationship_state_id,rendered_text_sha256,"
            "predicted_positive,prediction_basis,model_version) VALUES "
            "(:x,:r,:u,'build_tension',ARRAY['teasing'],:s,'sha',0.6,'b','v0')"
        ),
        {"x": uuid4(), "r": rel_id, "u": user_id, "s": sid},
    )
    await session.execute(
        text(
            "INSERT INTO memory_chunks (relationship_id,kind,summary) "
            "VALUES (:r,'inside_joke','the cat at the coffee shop')"
        ),
        {"r": rel_id},
    )
    await session.execute(
        text(
            "INSERT INTO preference_evidence (person_id,relationship_id,domain,"
            "key,direction,provenance,observed_at,source_interaction_id) VALUES "
            "(:p,:r,'flirt_mode','teasing','positive','observed',now(),:i)"
        ),
        {"p": person_id, "r": rel_id, "i": iid},
    )


async def test_forgetting_a_person_leaves_nothing(db, register, erasure):
    """And it does so without a BYPASSRLS credential anywhere on the path."""
    from sonder.storage.db import maintenance_session as admin_session
    from sonder.storage.deletion import forget_person, inspect_person

    user_id, person_id, rel_id = uuid4(), uuid4(), uuid4()
    async with admin_session() as s:
        await seed_person(s, user_id, person_id, rel_id)
    await register(user_id)

    async with erasure(user_id) as s:
        before = await inspect_person(s, person_id)
    assert before["found"]
    assert before["total_rows"] > 0
    assert "assertiveness" in before["inferred_traits"]

    async with erasure(user_id) as s:
        receipt = await forget_person(s, person_id)

    assert receipt.complete, f"residue remained: {receipt.residue}"
    assert receipt.relationships == 1
    assert sum(receipt.rows.values()) > 0

    async with erasure(user_id) as s:
        after = await inspect_person(s, person_id)
    assert not after["found"], "the inferred profile survived erasure"


async def test_deleting_the_relationship_alone_leaves_the_profile(db, register, erasure):
    """The bug this module exists for. Documented, not accidental."""
    from sqlalchemy import text as sqltext

    from sonder.storage.db import maintenance_session as admin_session
    from sonder.storage.deletion import orphaned_profiles

    user_id, person_id, rel_id = uuid4(), uuid4(), uuid4()
    async with admin_session() as s:
        await seed_person(s, user_id, person_id, rel_id, name="Leftover")
        await s.execute(sqltext("DELETE FROM relationships WHERE id = :r"), {"r": rel_id})
    await register(user_id)

    async with erasure(user_id) as s:
        left = await orphaned_profiles(s, user_id)
    assert any(p["display_name"] == "Leftover" for p in left), (
        "an orphaned profile must be surfaced, not silently retained"
    )
    assert all(
        p["reason"] == "no relationship" for p in left if p["display_name"] == "Leftover"
    )


async def test_erasure_is_scoped_to_one_person(db, register, erasure):
    """Erasing one person must not touch another."""
    from sonder.storage.db import maintenance_session as admin_session
    from sonder.storage.deletion import forget_person, inspect_person

    user_id = uuid4()
    p1, r1 = uuid4(), uuid4()
    p2, r2 = uuid4(), uuid4()
    async with admin_session() as s:
        await seed_person(s, user_id, p1, r1, name="Erase")
        await seed_person(s, user_id, p2, r2, name="Keep")

    await register(user_id)
    async with erasure(user_id) as s:
        await forget_person(s, p1)

    async with erasure(user_id) as s:
        kept = await inspect_person(s, p2)
    assert kept["found"]
    assert kept["total_rows"] > 0, "erasing one person damaged another's data"


async def test_expired_retention_is_surfaced_as_residue(db, register, erasure):
    from sqlalchemy import text as sqltext

    from sonder.storage.db import maintenance_session as admin_session
    from sonder.storage.deletion import orphaned_profiles

    user_id, person_id, rel_id = uuid4(), uuid4(), uuid4()
    async with admin_session() as s:
        await seed_person(s, user_id, person_id, rel_id, name="Expired")
        await s.execute(
            sqltext("UPDATE person_profiles SET retention_until = :t WHERE id = :p"),
            {"t": NOW - timedelta(days=1), "p": person_id},
        )

    await register(user_id)
    async with erasure(user_id) as s:
        left = await orphaned_profiles(s, user_id)
    assert any(p["display_name"] == "Expired" and p["reason"] == "expired" for p in left)


# ---------------------------------------------------------------- ownership


async def test_ownership_check_blocks_another_account(db, register):
    """The check now runs on the app role, under the user-scoped policies.

    It used to run on the BYPASSRLS maintenance role, which meant the only
    thing standing between one account and another was this comparison. Now
    the query cannot see the other account's row at all.
    """
    from fastapi import HTTPException

    from sonder.api.auth import Identity, owned_person, owned_relationship
    from sonder.storage.db import maintenance_session as admin_session

    owner, intruder = uuid4(), uuid4()
    person_id, rel_id = uuid4(), uuid4()
    async with admin_session() as s:
        await seed_person(s, owner, person_id, rel_id, name="Private")
        await s.execute(
            text("INSERT INTO users (id) VALUES (:u) ON CONFLICT DO NOTHING"),
            {"u": intruder},
        )
    owner_identity = Identity(owner, await register(owner))
    intruder_identity = Identity(intruder, await register(intruder))

    assert await owned_relationship(rel_id, owner_identity) == rel_id
    assert await owned_person(person_id, owner_identity) == person_id

    with pytest.raises(HTTPException) as e1:
        await owned_relationship(rel_id, intruder_identity)
    assert e1.value.status_code == 404, "must not disclose that it exists"

    with pytest.raises(HTTPException) as e2:
        await owned_person(person_id, intruder_identity)
    assert e2.value.status_code == 404


async def test_bearer_tokens_are_stored_hashed(db):
    from fastapi import HTTPException

    from sonder.api.auth import current_identity, hash_token, mint_token
    from sonder.storage.db import maintenance_session as admin_session

    user_id = uuid4()
    plaintext, digest = mint_token()
    async with admin_session() as s:
        await s.execute(
            text("INSERT INTO users (id) VALUES (:u) ON CONFLICT DO NOTHING"),
            {"u": user_id},
        )
        await s.execute(
            text(
                "INSERT INTO api_tokens (user_id, token_sha256, label) "
                "VALUES (:u,:d,'test')"
            ),
            {"u": user_id, "d": digest},
        )

    assert (await current_identity(f"Bearer {plaintext}")).user_id == user_id

    async with admin_session() as s:
        stored = (
            await s.execute(
                text("SELECT token_sha256 FROM api_tokens WHERE user_id=:u"),
                {"u": user_id},
            )
        ).scalar()
    assert stored != plaintext and stored == hash_token(plaintext)

    for bad in (None, "Bearer wrong", "Basic x"):
        with pytest.raises(HTTPException) as e:
            await current_identity(bad)
        assert e.value.status_code == 401


async def test_revoked_token_stops_working(db):
    from fastapi import HTTPException

    from sonder.api.auth import current_identity, mint_token
    from sonder.storage.db import maintenance_session as admin_session

    user_id = uuid4()
    plaintext, digest = mint_token()
    async with admin_session() as s:
        await s.execute(
            text("INSERT INTO users (id) VALUES (:u) ON CONFLICT DO NOTHING"),
            {"u": user_id},
        )
        await s.execute(
            text(
                "INSERT INTO api_tokens (user_id, token_sha256, revoked_at) "
                "VALUES (:u,:d, now())"
            ),
            {"u": user_id, "d": digest},
        )
    with pytest.raises(HTTPException) as e:
        await current_identity(f"Bearer {plaintext}")
    assert e.value.status_code == 401


async def test_every_dyad_table_cascades_from_relationships(db):
    """The invariant the post-deletion sweep used to approximate.

    `forget_person` deletes the relationship and relies on the cascade to take
    the dyad rows with it. Under the erasure role the sweep cannot verify that
    -- deleting the relationship also removes the rows from view -- so the
    property is asserted here instead, against the real schema. A dyad table
    added later without ON DELETE CASCADE fails here rather than silently
    leaving inferred data behind a receipt that says "complete".
    """
    from sonder.storage.db import maintenance_session
    from sonder.storage.deletion import DYAD_TABLES

    expected = {name for name, _ in DYAD_TABLES if name != "messages"}
    async with maintenance_session() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT c.conrelid::regclass::text, c.confdeltype::text "
                    "  FROM pg_constraint c "
                    " WHERE c.contype = 'f' "
                    "   AND c.confrelid = 'relationships'::regclass"
                )
            )
        ).all()
    cascading = {table for table, deltype in rows if deltype == "c"}
    missing = expected - cascading
    assert not missing, f"these dyad tables do not cascade from relationships: {missing}"

    # messages hang off interactions, which cascades from relationships.
    async with maintenance_session() as s:
        deltype = (
            (
                await s.execute(
                    text(
                        "SELECT c.confdeltype::text FROM pg_constraint c "
                        " WHERE c.contype = 'f' AND c.conrelid = 'messages'::regclass "
                        "   AND c.confrelid = 'interactions'::regclass"
                    )
                )
            )
            .scalars()
            .first()
        )
    assert deltype == "c", "messages do not cascade from interactions"


async def test_erasure_actually_removes_the_dyad_rows(db, register, erasure):
    """The sweep cannot see this, so check it with a role that can."""
    from sonder.storage.db import maintenance_session
    from sonder.storage.deletion import forget_person

    user_id, person_id, rel_id = uuid4(), uuid4(), uuid4()
    async with maintenance_session() as s:
        await seed_person(s, user_id, person_id, rel_id, name="Gone")

    async with maintenance_session() as s:
        before = (
            await s.execute(
                text("SELECT count(*) FROM interactions WHERE relationship_id = :r"),
                {"r": rel_id},
            )
        ).scalar()
    assert before > 0

    await register(user_id)
    async with erasure(user_id) as s:
        receipt = await forget_person(s, person_id)
    assert receipt.complete

    async with maintenance_session() as s:
        for table in ("interactions", "signals", "relationship_states", "memory_chunks"):
            left = (
                await s.execute(
                    text(f"SELECT count(*) FROM {table} WHERE relationship_id = :r"),
                    {"r": rel_id},
                )
            ).scalar()
            assert left == 0, f"{table} survived erasure"
        messages = (
            await s.execute(
                text(
                    "SELECT count(*) FROM messages m JOIN interactions i "
                    "ON i.id = m.interaction_id WHERE i.relationship_id = :r"
                ),
                {"r": rel_id},
            )
        ).scalar()
        assert messages == 0, "messages survived erasure"


async def test_erasure_removes_every_version_of_the_profile(db, register, erasure):
    """`person_profiles` is versioned, and each version keeps its own traits.

    Erasing only the row whose id was passed leaves every earlier inference
    about that person on disk behind a receipt that says "complete" — which is
    worse than not erasing, because the user stops thinking about it.
    """
    from sonder.storage.db import maintenance_session
    from sonder.storage.deletion import forget_person

    user_id, v1, v2, rel_id = uuid4(), uuid4(), uuid4(), uuid4()
    async with maintenance_session() as s:
        await seed_person(s, user_id, v1, rel_id, name="Versioned")
        # A second snapshot, superseding the first, as the profile updater
        # would write it.
        await s.execute(
            text(
                "INSERT INTO person_profiles (id,user_id,subject,data_subject,"
                "display_name,version,supersedes,retention_until,traits) VALUES "
                "(:v2,:u,'target','third_party','Versioned',2,:v1,:r,:t)"
            ),
            {
                "v2": v2,
                "u": user_id,
                "v1": v1,
                "r": NOW + timedelta(days=365),
                "t": '{"assertiveness": {"value": 0.9, "confidence": 0.8}}',
            },
        )

    await register(user_id)
    async with erasure(user_id) as s:
        receipt = await forget_person(s, v2)
    assert receipt.complete, receipt.residue

    async with maintenance_session() as s:
        left = (
            await s.execute(
                text("SELECT count(*) FROM person_profiles WHERE id IN (:a, :b)"),
                {"a": v1, "b": v2},
            )
        ).scalar()
    assert left == 0, "a superseded profile version survived erasure"
