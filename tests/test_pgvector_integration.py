"""Real pgvector contract tests.

These are skipped during the fast unit pass, then run by scripts/check.sh
against the temporary pgvector/pgvector:pg16 stack.
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import StatementError

DB_URL = os.environ.get("SONDER_TEST_DATABASE_URL")
pytestmark = [
    pytest.mark.skipif(not DB_URL, reason="SONDER_TEST_DATABASE_URL not set"),
    pytest.mark.asyncio,
]


async def _seed_relationship(session, user_id, person_id, relationship_id, name):
    await session.execute(
        text("INSERT INTO users (id) VALUES (:id) ON CONFLICT DO NOTHING"),
        {"id": user_id},
    )
    await session.execute(
        text(
            "INSERT INTO person_profiles "
            "(id,user_id,subject,data_subject,display_name,version,retention_until) "
            "VALUES (:person,:user,'target','third_party',:name,1,"
            "now() + interval '1 year')"
        ),
        {"person": person_id, "user": user_id, "name": name},
    )
    await session.execute(
        text(
            "INSERT INTO relationships (id,user_id,target_person_id) "
            "VALUES (:relationship,:user,:person)"
        ),
        {"relationship": relationship_id, "user": user_id, "person": person_id},
    )


def _embedding(first: float, second: float = 0.0) -> list[float]:
    return [first, second, *([0.0] * 1534)]


async def test_pgvector_schema_contract(db, register):
    from sonder.storage.db import dyad_session
    from sonder.storage.scope import Scope

    user_id = uuid4()
    await register(user_id)
    scope = Scope(user_id, uuid4())
    async with dyad_session(scope) as session:
        version = (
            await session.execute(
                text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            )
        ).scalar_one()
        column = (
            await session.execute(
                text(
                    "SELECT format_type(a.atttypid, a.atttypmod) "
                    "FROM pg_attribute a "
                    "WHERE a.attrelid = 'memory_chunks'::regclass "
                    "AND a.attname = 'embedding'"
                )
            )
        ).scalar_one()
        indexes = (
            await session.execute(
                text(
                    "SELECT pg_get_indexdef(i.indexrelid), i.indisvalid "
                    "FROM pg_index i "
                    "WHERE i.indrelid = 'memory_chunks'::regclass"
                )
            )
        ).all()

    assert version
    assert column == "vector(1536)"
    assert any(
        valid and "vector_cosine_ops" in definition and "ivfflat" in definition
        for definition, valid in indexes
    )


async def test_memory_search_orders_and_isolates_relationships(db, register):
    from sonder.storage.db import dyad_session
    from sonder.storage.repositories import MemoryRepo
    from sonder.storage.scope import Scope

    user_id = uuid4()
    person_a, person_b = uuid4(), uuid4()
    relationship_a, relationship_b = uuid4(), uuid4()
    await register(user_id)
    scope_a = Scope(user_id, relationship_a)
    scope_b = Scope(user_id, relationship_b)

    async with dyad_session(scope_a) as session:
        await _seed_relationship(session, user_id, person_a, relationship_a, "A")
        repo = MemoryRepo(session)
        await repo.add(scope_a, "fact", "nearest", _embedding(1.0))
        await repo.add(scope_a, "fact", "second", _embedding(0.8, 0.6))

    async with dyad_session(scope_b) as session:
        await _seed_relationship(session, user_id, person_b, relationship_b, "B")
        await MemoryRepo(session).add(
            scope_b, "fact", "foreign exact match", _embedding(1.0)
        )

    async with dyad_session(scope_a) as session:
        rows = await MemoryRepo(session).search(scope_a, _embedding(1.0), k=10)
        dimensions = (
            (
                await session.execute(
                    text("SELECT vector_dims(embedding) FROM memory_chunks")
                )
            )
            .scalars()
            .all()
        )

    assert [row.summary for row in rows] == ["nearest", "second"]
    assert dimensions == [1536, 1536]


async def test_wrong_embedding_dimension_fails(db, register):
    from sonder.storage.db import dyad_session
    from sonder.storage.repositories import MemoryRepo
    from sonder.storage.scope import Scope

    user_id, person_id, relationship_id = uuid4(), uuid4(), uuid4()
    await register(user_id)
    scope = Scope(user_id, relationship_id)
    with pytest.raises(
        StatementError, match="1536 dimensions|different vector dimensions"
    ):
        async with dyad_session(scope) as session:
            await _seed_relationship(session, user_id, person_id, relationship_id, "C")
            await MemoryRepo(session).add(scope, "fact", "wrong dimension", [0.0] * 1535)
