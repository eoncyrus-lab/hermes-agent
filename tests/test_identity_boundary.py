"""The identity boundary, against a real Postgres.

Everything here is about one claim: that setting `app.user_id` proves nothing.
Before this, a session could name any user id it liked and the user-scoped
tables had no policy to disagree with it -- the only thing standing in the way
was an application WHERE clause, on a connection that could also bypass RLS
entirely. These tests are the ones that would have caught that.
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


async def _seed(session, user_id, person_id, rel_id, name="T"):
    await session.execute(
        text("INSERT INTO users (id) VALUES (:u) ON CONFLICT DO NOTHING"),
        {"u": user_id},
    )
    await session.execute(
        text(
            "INSERT INTO person_profiles (id,user_id,subject,data_subject,"
            "display_name,version,retention_until) VALUES "
            "(:p,:u,'target','third_party',:n,1,:r)"
        ),
        {"p": person_id, "u": user_id, "n": name, "r": NOW + timedelta(days=365)},
    )
    await session.execute(
        text("INSERT INTO relationships (id,user_id,target_person_id) VALUES (:r,:u,:p)"),
        {"r": rel_id, "u": user_id, "p": person_id},
    )
    await session.execute(
        text(
            "INSERT INTO memory_chunks (relationship_id,kind,summary) "
            "VALUES (:r,'inside_joke',:s)"
        ),
        {"r": rel_id, "s": f"{name} secret"},
    )


# ------------------------------------------------------------------ identity


async def test_a_user_id_without_a_matching_claim_sees_nothing(db, register):
    """The attack the claim exists to stop: a handler naming someone else."""
    from sonder.storage.db import maintenance_session, user_session
    from sonder.storage.scope import UserScope

    victim, attacker = uuid4(), uuid4()
    async with maintenance_session() as s:
        await _seed(s, victim, uuid4(), uuid4(), "Victim")
        await _seed(s, attacker, uuid4(), uuid4(), "Attacker")

    attacker_claim = await register(attacker)
    await register(victim)

    # The attacker's own credential, pointed at the victim's user id.
    spoofed = UserScope(user_id=victim, identity_claim=attacker_claim)
    async with user_session(spoofed) as s:
        rows = (await s.execute(text("SELECT count(*) FROM relationships"))).scalar()
    assert rows == 0, "a mismatched claim authorised a foreign user id"


async def test_the_claim_alone_scopes_to_its_own_account(db, register):
    from sonder.storage.db import maintenance_session, user_session
    from sonder.storage.scope import UserScope

    mine, theirs = uuid4(), uuid4()
    async with maintenance_session() as s:
        await _seed(s, mine, uuid4(), uuid4(), "Mine")
        await _seed(s, theirs, uuid4(), uuid4(), "Theirs")
    claim = await register(mine)

    async with user_session(UserScope(mine, claim)) as s:
        names = [
            row[0]
            for row in (
                await s.execute(text("SELECT display_name FROM person_profiles"))
            ).all()
        ]
    assert names == ["Mine"]


async def test_a_revoked_token_stops_authorising_immediately(db, register):
    from sonder.storage.db import maintenance_session, user_session
    from sonder.storage.scope import UserScope

    user_id = uuid4()
    async with maintenance_session() as s:
        await _seed(s, user_id, uuid4(), uuid4(), "Revocable")
    claim = await register(user_id)

    async with user_session(UserScope(user_id, claim)) as s:
        assert (await s.execute(text("SELECT count(*) FROM relationships"))).scalar() == 1

    async with maintenance_session() as s:
        await s.execute(
            text("UPDATE api_tokens SET revoked_at = now() WHERE token_sha256 = :d"),
            {"d": claim},
        )

    # No cache to invalidate: the policy re-reads the token every statement.
    async with user_session(UserScope(user_id, claim)) as s:
        assert (await s.execute(text("SELECT count(*) FROM relationships"))).scalar() == 0


async def test_the_request_role_cannot_read_the_token_table(db, register):
    """Verifying a digest you hold is a smaller capability than listing them."""
    from sqlalchemy.exc import ProgrammingError

    from sonder.storage.db import user_session
    from sonder.storage.scope import UserScope

    user_id = uuid4()
    claim = await register(user_id)
    with pytest.raises(ProgrammingError, match="permission denied"):
        async with user_session(UserScope(user_id, claim)) as s:
            await s.execute(text("SELECT token_sha256 FROM api_tokens"))


async def test_the_identity_role_can_only_authenticate(db, register):
    from sqlalchemy.exc import ProgrammingError

    from sonder.storage.db import identity_session

    if not os.environ.get("SONDER_IDENTITY_DATABASE_URL"):
        pytest.skip("SONDER_IDENTITY_DATABASE_URL not set")

    user_id = uuid4()
    claim = await register(user_id)

    async with identity_session() as s:
        row = (
            await s.execute(
                text("SELECT user_id, identity_claim FROM auth_bearer(:d)"),
                {"d": claim},
            )
        ).first()
    assert row is not None and row.user_id == user_id

    for table in ("users", "relationships", "person_profiles", "memory_chunks"):
        with pytest.raises(ProgrammingError, match="permission denied"):
            async with identity_session() as s:
                await s.execute(text(f"SELECT count(*) FROM {table}"))


# ----------------------------------------------------------------- the dyad


async def test_pinning_someone_elses_relationship_reveals_nothing(db, register):
    """Knowing a relationship id used to be enough. It no longer is."""
    from sonder.storage.db import dyad_session, maintenance_session
    from sonder.storage.scope import Scope

    victim, attacker = uuid4(), uuid4()
    victim_rel = uuid4()
    async with maintenance_session() as s:
        await _seed(s, victim, uuid4(), victim_rel, "Victim")
        await _seed(s, attacker, uuid4(), uuid4(), "Attacker")
    attacker_claim = await register(attacker)

    async with dyad_session(Scope(attacker, victim_rel, attacker_claim)) as s:
        leaked = (await s.execute(text("SELECT count(*) FROM memory_chunks"))).scalar()
    assert leaked == 0, "a pinned foreign relationship leaked its memory"


# ---------------------------------------------------------------- erasure role


async def test_the_erasure_role_cannot_reach_another_account(db, register):
    from sonder.storage.db import erasure_session, maintenance_session
    from sonder.storage.scope import UserScope

    mine, theirs = uuid4(), uuid4()
    async with maintenance_session() as s:
        await _seed(s, mine, uuid4(), uuid4(), "Mine")
        await _seed(s, theirs, uuid4(), uuid4(), "Theirs")
    claim = await register(mine)

    async with erasure_session(UserScope(mine, claim)) as s:
        chunks = (await s.execute(text("SELECT summary FROM memory_chunks"))).scalars()
        assert list(chunks) == ["Mine secret"]


async def test_the_erasure_role_holds_no_bypass_privilege(db, register):
    from sonder.storage.db import erasure_session
    from sonder.storage.scope import UserScope

    user_id = uuid4()
    claim = await register(user_id)
    async with erasure_session(UserScope(user_id, claim)) as s:
        row = (
            await s.execute(
                text(
                    "SELECT rolbypassrls, rolsuper FROM pg_roles "
                    "WHERE rolname = current_user"
                )
            )
        ).one()
    assert row.rolbypassrls is False
    assert row.rolsuper is False


# ----------------------------------------------------------------- the worker


async def test_a_job_naming_a_relationship_its_user_does_not_own_is_refused(db):
    """The queue is a second way into the pipeline, so it is checked too."""
    from sonder.storage.db import maintenance_session, worker_session
    from sonder.storage.scope import Scope
    from sonder.worker.tasks import OwnershipMismatch, authorize

    owner, impostor = uuid4(), uuid4()
    rel_id = uuid4()
    async with maintenance_session() as s:
        await _seed(s, owner, uuid4(), rel_id, "Owned")
        await s.execute(
            text("INSERT INTO users (id) VALUES (:u) ON CONFLICT DO NOTHING"),
            {"u": impostor},
        )

    async with worker_session(Scope(owner, rel_id)) as s:
        assert await authorize(s, Scope(owner, rel_id)) is not None

    with pytest.raises(OwnershipMismatch):
        async with worker_session(Scope(impostor, rel_id)) as s:
            await authorize(s, Scope(impostor, rel_id))


async def test_the_worker_role_holds_no_bypass_privilege(db):
    from sonder.storage.db import worker_session
    from sonder.storage.scope import Scope

    async with worker_session(Scope(uuid4(), uuid4())) as s:
        row = (
            await s.execute(
                text(
                    "SELECT rolbypassrls, rolsuper FROM pg_roles "
                    "WHERE rolname = current_user"
                )
            )
        ).one()
    assert row.rolbypassrls is False
    assert row.rolsuper is False


async def test_an_unpinned_worker_session_reads_nothing(db):
    """A job that forgets to pin gets zero rows, not someone else's."""
    from sonder.storage.db import maintenance_session, worker_session
    from sonder.storage.scope import Scope

    user_id, rel_id = uuid4(), uuid4()
    async with maintenance_session() as s:
        await _seed(s, user_id, uuid4(), rel_id, "Pinned")

    async with worker_session(Scope(user_id, rel_id)) as s:
        await s.execute(text("SELECT set_config('app.relationship_id', '', true)"))
        leaked = (await s.execute(text("SELECT count(*) FROM memory_chunks"))).scalar()
    assert leaked == 0


# ------------------------------------------------------------- idempotency


def _state(user_id, person_id, rel_id, version=1, supersedes=None, sources=()):
    from sonder.state.relationship import compute

    return compute(
        [],
        relationship_id=rel_id,
        user_id=user_id,
        target_person_id=person_id,
        version=version,
        supersedes=supersedes,
        interaction_ids=list(sources),
        now=NOW,
    )


async def _interaction(session, rel_id):
    iid = uuid4()
    await session.execute(
        text(
            "INSERT INTO interactions (id,relationship_id,channel,source,"
            "started_at,extractor_version) VALUES "
            "(:i,:r,'wechat','manual_entry',now(),'t')"
        ),
        {"i": iid, "r": rel_id},
    )
    return iid


async def test_replaying_a_state_computation_writes_one_row(db, register):
    """An ARQ retry must converge, not append an identical snapshot."""
    from sonder.storage.db import dyad_session, maintenance_session
    from sonder.storage.repositories import StateRepo
    from sonder.storage.scope import Scope

    user_id, person_id, rel_id = uuid4(), uuid4(), uuid4()
    async with maintenance_session() as s:
        await _seed(s, user_id, person_id, rel_id, "Replay")
    claim = await register(user_id)
    scope = Scope(user_id, rel_id, claim)

    first = _state(user_id, person_id, rel_id)
    async with dyad_session(scope) as s:
        first_id = await StateRepo(s).append(scope, first, input_fingerprint="same")

    # A redelivery recomputes from the same inputs and gets a fresh object id.
    second = _state(user_id, person_id, rel_id)
    assert second.id != first.id
    async with dyad_session(scope) as s:
        second_id = await StateRepo(s).append(scope, second, input_fingerprint="same")

    assert second_id == first_id, "a replay wrote a second snapshot"
    async with dyad_session(scope) as s:
        count = (
            await s.execute(text("SELECT count(*) FROM relationship_states"))
        ).scalar()
    assert count == 1


async def test_different_inputs_still_write_a_new_state(db, register):
    from sonder.storage.db import dyad_session, maintenance_session
    from sonder.storage.repositories import StateRepo
    from sonder.storage.scope import Scope

    user_id, person_id, rel_id = uuid4(), uuid4(), uuid4()
    async with maintenance_session() as s:
        await _seed(s, user_id, person_id, rel_id, "Moving")
        iid = await _interaction(s, rel_id)
    claim = await register(user_id)
    scope = Scope(user_id, rel_id, claim)

    async with dyad_session(scope) as s:
        first = await StateRepo(s).append(
            scope, _state(user_id, person_id, rel_id), input_fingerprint="inputs-1"
        )
    async with dyad_session(scope) as s:
        second = await StateRepo(s).append(
            scope,
            _state(
                user_id,
                person_id,
                rel_id,
                version=2,
                supersedes=first,
                sources=(iid,),
            ),
            input_fingerprint="inputs-2",
        )

    assert second != first
    async with dyad_session(scope) as s:
        count = (
            await s.execute(text("SELECT count(*) FROM relationship_states"))
        ).scalar()
    assert count == 2


async def test_a_snapshot_cannot_supersede_another_relationships_snapshot(db, register):
    from sqlalchemy.exc import IntegrityError

    from sonder.storage.db import maintenance_session

    user_id = uuid4()
    p1, r1, p2, r2 = uuid4(), uuid4(), uuid4(), uuid4()
    foreign = uuid4()
    async with maintenance_session() as s:
        await _seed(s, user_id, p1, r1, "One")
        await _seed(s, user_id, p2, r2, "Two")
        await s.execute(
            text(
                "INSERT INTO relationship_states (id,relationship_id,version,as_of,"
                "stage,model_version,input_fingerprint) VALUES "
                "(:s,:r,1,now(),'s','v','fp-a')"
            ),
            {"s": foreign, "r": r1},
        )

    with pytest.raises(IntegrityError):
        async with maintenance_session() as s:
            await s.execute(
                text(
                    "INSERT INTO relationship_states (id,relationship_id,version,"
                    "as_of,supersedes,stage,model_version,input_fingerprint) VALUES "
                    "(:s,:r,2,now(),:sup,'s','v','fp-b')"
                ),
                {"s": uuid4(), "r": r2, "sup": foreign},
            )


# ------------------------------------------------------------ migration order


async def test_replaying_a_migration_does_not_give_a_privilege_back(db, superuser):
    """002 is documented as rerunnable, and it must not undo 003.

    A blanket `GRANT ... ON ALL TABLES ... TO sonder_app` in the earlier file
    silently re-opened `api_tokens` every time an operator replayed it --
    handing a request-path role the ability to mint itself a token or clear
    `revoked_at`, which is exactly what the identity claim relies on being
    impossible.
    """
    from pathlib import Path

    from sonder.storage.db import maintenance_session

    migrations = Path(__file__).parents[1] / "migrations"

    async def app_can_read_tokens() -> bool:
        async with maintenance_session() as s:
            return (
                await s.execute(
                    text(
                        "SELECT has_table_privilege('sonder_app', 'api_tokens', 'SELECT')"
                    )
                )
            ).scalar()

    assert await app_can_read_tokens() is False

    for name in (
        "002_security_boundaries.sql",
        "003_identity_and_idempotency.sql",
        "002_security_boundaries.sql",
    ):
        await superuser((migrations / name).read_text())
        assert await app_can_read_tokens() is False, f"{name} re-opened api_tokens"

    # And the role still reaches everything it is supposed to.
    async with maintenance_session() as s:
        for table in ("relationships", "interactions", "memory_chunks"):
            assert (
                await s.execute(
                    text(f"SELECT has_table_privilege('sonder_app', '{table}', 'SELECT')")
                )
            ).scalar() is True


async def test_only_the_bot_role_can_trade_a_telegram_id_for_a_claim(db, register):
    """A Telegram id is ten guessable digits; a token digest is not.

    Whatever can call auth_telegram can walk the id space and turn a hit into
    a working claim, so it lives on a role the API never holds.
    """
    from sonder.storage.db import maintenance_session

    async with maintenance_session() as s:
        grantees = {
            row[0]
            for row in (
                await s.execute(
                    text(
                        "SELECT r.rolname FROM pg_proc p, pg_roles r "
                        " WHERE p.proname = 'auth_telegram' "
                        "   AND has_function_privilege("
                        "         r.rolname, p.oid, 'EXECUTE') "
                        "   AND r.rolname LIKE 'sonder\\_%'"
                    )
                )
            ).all()
        }
    assert "sonder_identity" not in grantees, "the API's role can impersonate by id"
    assert "sonder_app" not in grantees
    assert "sonder_erase" not in grantees
    assert "sonder_worker" not in grantees
    assert "sonder_bot" in grantees, "the bot cannot authenticate its own chats"


async def test_the_ttl_purge_can_actually_clear_a_source_artifact(db, register):
    """The retention promise depends on an UPDATE the schema used to forbid."""
    from sonder.storage.db import maintenance_session
    from sonder.worker.tasks import purge_artifacts

    user_id, rel_id = uuid4(), uuid4()
    async with maintenance_session() as s:
        await _seed(s, user_id, uuid4(), rel_id, "Purgeable")
        await s.execute(
            text(
                "INSERT INTO interactions (relationship_id,channel,source,"
                "started_at,extractor_version,source_artifact_id,ingested_at) "
                "VALUES (:r,'wechat','manual_entry',now(),'t',gen_random_uuid(),"
                "now() - interval '30 days')"
            ),
            {"r": rel_id},
        )

    result = await purge_artifacts({})
    assert result["purged"] >= 1

    async with maintenance_session() as s:
        remaining = (
            await s.execute(
                text(
                    "SELECT count(*) FROM interactions "
                    " WHERE relationship_id = :r AND source_artifact_id IS NOT NULL"
                ),
                {"r": rel_id},
            )
        ).scalar()
    assert remaining == 0


async def test_history_is_still_immutable_after_the_purge_exemption(db, register):
    """The narrowed trigger must permit the purge and nothing else."""
    from sqlalchemy.exc import DBAPIError

    from sonder.storage.db import maintenance_session

    user_id, rel_id = uuid4(), uuid4()
    async with maintenance_session() as s:
        await _seed(s, user_id, uuid4(), rel_id, "Immutable")
        iid = await _interaction(s, rel_id)

    with pytest.raises(DBAPIError, match="append-only"):
        async with maintenance_session() as s:
            await s.execute(
                text("UPDATE interactions SET message_count_self = 99 WHERE id = :i"),
                {"i": iid},
            )


async def test_the_purge_exemption_guards_columns_added_later(db, superuser):
    """The guard compares by column name, not by position.

    A positional comparison stops covering the table the moment someone adds a
    column, and does so silently -- the purge UPDATE would then become a
    general-purpose write into whatever was added.
    """
    from sqlalchemy.exc import DBAPIError

    from sonder.storage.db import maintenance_session

    user_id, rel_id = uuid4(), uuid4()
    async with maintenance_session() as s:
        await _seed(s, user_id, uuid4(), rel_id, "Guarded")
        iid = await _interaction(s, rel_id)

    await superuser("ALTER TABLE interactions ADD COLUMN smuggled text;")
    try:
        with pytest.raises(DBAPIError, match="append-only"):
            async with maintenance_session() as s:
                await s.execute(
                    text(
                        "UPDATE interactions SET source_artifact_id = NULL, "
                        "source_artifact_deleted = true, smuggled = 'x' "
                        " WHERE id = :i"
                    ),
                    {"i": iid},
                )
        # The purge itself is still allowed.
        async with maintenance_session() as s:
            await s.execute(
                text(
                    "UPDATE interactions SET source_artifact_id = NULL, "
                    "source_artifact_deleted = true WHERE id = :i"
                ),
                {"i": iid},
            )
    finally:
        await superuser("ALTER TABLE interactions DROP COLUMN IF EXISTS smuggled;")


async def test_a_table_added_after_the_migrations_is_readable_by_the_app_role(
    db, superuser
):
    """Default privileges must still cover the request-path role.

    Dropping `sonder_app` from `ALTER DEFAULT PRIVILEGES` closes nothing --
    defaults only ever apply to tables that do not exist yet, so `api_tokens`
    can never be caught by them -- but it does mean the next migration that
    adds a table leaves every API path unable to read it.
    """
    from sonder.storage.db import maintenance_session

    await superuser("CREATE TABLE IF NOT EXISTS later_table (id int primary key);")
    try:
        async with maintenance_session() as s:
            readable = {
                role: (
                    await s.execute(
                        text(
                            "SELECT has_table_privilege("
                            f"'{role}', 'later_table', 'SELECT')"
                        )
                    )
                ).scalar()
                for role in ("sonder_app", "sonder_erase", "sonder_worker")
            }
        assert readable == {
            "sonder_app": True,
            "sonder_erase": True,
            "sonder_worker": True,
        }
    finally:
        await superuser("DROP TABLE IF EXISTS later_table;")


async def test_a_conflict_that_is_not_the_fingerprint_is_reported_where_it_happens(
    db, register
):
    """An untargeted ON CONFLICT would surface this as a missing row.

    `preference_models` still carries `UNIQUE (person_id, version)` from 001,
    and one person can be the target of two of the same user's relationships
    -- each numbering its models from 1. That must raise the integrity error
    it is, not `NoResultFound` from an unrelated re-select.
    """
    from sqlalchemy.exc import IntegrityError

    from sonder.storage.db import dyad_session, maintenance_session
    from sonder.storage.repositories import PreferenceRepo
    from sonder.storage.scope import Scope

    user_id, person_id = uuid4(), uuid4()
    rel_a, rel_b = uuid4(), uuid4()
    async with maintenance_session() as s:
        await _seed(s, user_id, person_id, rel_a, "Shared")
        # A second relationship to the same person.
        await s.execute(
            text(
                "INSERT INTO relationships (id,user_id,target_person_id) "
                "VALUES (:r,:u,:p)"
            ),
            {"r": rel_b, "u": user_id, "p": person_id},
        )
        await s.execute(
            text(
                "INSERT INTO preference_models (person_id,relationship_id,version,"
                "scorer_version,input_fingerprint) "
                "VALUES (:p,:r,1,'v','fp-a')"
            ),
            {"p": person_id, "r": rel_a},
        )
    claim = await register(user_id)

    from sonder.domain.schemas.preference import PreferenceModel

    model = PreferenceModel(
        person_id=person_id,
        relationship_id=rel_b,
        version=1,
        entries={},
        scorer_version="v",
    )
    scope = Scope(user_id, rel_b, claim)
    with pytest.raises(IntegrityError):
        async with dyad_session(scope) as s:
            await PreferenceRepo(s).save_model(scope, model, input_fingerprint="fp-b")
