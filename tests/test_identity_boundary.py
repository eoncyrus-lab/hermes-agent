"""The identity boundary, against a real Postgres.

Everything here is about one claim: that setting `app.user_id` proves nothing.
Before this, a session could name any user id it liked and the user-scoped
tables had no policy to disagree with it -- the only thing standing in the way
was an application WHERE clause, on a connection that could also bypass RLS
entirely. These tests are the ones that would have caught that.
"""

from __future__ import annotations

import hashlib
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
    digest = hashlib.sha256(f"test-token:{user_id}".encode()).hexdigest()

    async with user_session(UserScope(user_id, claim)) as s:
        assert (await s.execute(text("SELECT count(*) FROM relationships"))).scalar() == 1

    async with maintenance_session() as s:
        await s.execute(
            text("UPDATE api_tokens SET revoked_at = now() WHERE token_sha256 = :d"),
            {"d": digest},
        )

    # No cache to invalidate: the policy re-reads the token every statement.
    async with user_session(UserScope(user_id, claim)) as s:
        assert (await s.execute(text("SELECT count(*) FROM relationships"))).scalar() == 0

    from sonder.storage.db import identity_session

    async with identity_session() as s:
        assert (
            await s.execute(text("SELECT * FROM auth_bearer(:d)"), {"d": digest})
        ).first() is None


async def test_a_forged_or_expired_claim_sees_nothing(db, register):
    from sonder.storage.db import maintenance_session, user_session
    from sonder.storage.scope import UserScope

    user_id = uuid4()
    async with maintenance_session() as s:
        await _seed(s, user_id, uuid4(), uuid4(), "Signed")
    claim = await register(user_id)

    kind, source, expires, signature = claim.split(":")
    forged = ":".join((kind, source, expires, "0" * len(signature)))
    expired = ":".join((kind, source, "1", signature))
    for bad_claim in (forged, expired):
        async with user_session(UserScope(user_id, bad_claim)) as s:
            assert (
                await s.execute(text("SELECT count(*) FROM relationships"))
            ).scalar() == 0


async def test_the_request_role_cannot_read_identity_or_vault_tables(db, register):
    """Ordinary request SQL cannot list credentials or vault ciphertext."""
    from sqlalchemy.exc import ProgrammingError

    from sonder.storage.db import user_session
    from sonder.storage.scope import UserScope

    user_id = uuid4()
    claim = await register(user_id)
    for statement in (
        "SELECT token_sha256 FROM api_tokens",
        "SELECT ciphertext FROM vault_records",
        "SELECT telegram_user_id FROM telegram_accounts",
    ):
        with pytest.raises(ProgrammingError, match="permission denied"):
            async with user_session(UserScope(user_id, claim)) as s:
                await s.execute(text(statement))


async def test_the_identity_role_can_only_authenticate(db, register):
    from sqlalchemy.exc import ProgrammingError

    from sonder.storage.db import identity_session

    if not os.environ.get("SONDER_IDENTITY_DATABASE_URL"):
        pytest.skip("SONDER_IDENTITY_DATABASE_URL not set")

    user_id = uuid4()
    await register(user_id)
    digest = hashlib.sha256(f"test-token:{user_id}".encode()).hexdigest()

    async with identity_session() as s:
        row = (
            await s.execute(
                text("SELECT user_id, identity_claim FROM auth_bearer(:d)"),
                {"d": digest},
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


async def test_telegram_auth_does_not_depend_on_an_api_token(db):
    from sqlalchemy.ext.asyncio import create_async_engine

    from sonder.storage.db import maintenance_session, user_session
    from sonder.storage.scope import UserScope

    bot_url = os.environ.get("SONDER_BOT_DATABASE_URL")
    if not bot_url:
        pytest.skip("SONDER_BOT_DATABASE_URL not set")

    # Not a constant: telegram_user_id is the primary key, so a fixed value
    # leaves a row behind and the second run against the same database fails
    # on it. The DB suite has to be re-runnable -- that is how it is used.
    user_id, telegram_id = uuid4(), uuid4().int % 10**9
    async with maintenance_session() as s:
        await _seed(s, user_id, uuid4(), uuid4(), "Telegram")
        await s.execute(
            text(
                "INSERT INTO telegram_accounts (telegram_user_id,user_id) VALUES (:tg,:u)"
            ),
            {"tg": telegram_id, "u": user_id},
        )

    engine = create_async_engine(bot_url)
    try:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text("SELECT * FROM auth_telegram(:tg)"), {"tg": telegram_id}
                )
            ).first()
    finally:
        await engine.dispose()

    assert row is not None and row.user_id == user_id
    async with user_session(UserScope(user_id, row.identity_claim)) as s:
        assert (await s.execute(text("SELECT count(*) FROM relationships"))).scalar() == 1


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


async def test_a_table_added_after_the_migrations_gets_no_implicit_service_grant(
    db, superuser
):
    """A new sensitive table requires a deliberate role grant and policy."""
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
            "sonder_app": False,
            "sonder_erase": False,
            "sonder_worker": False,
        }
    finally:
        await superuser("DROP TABLE IF EXISTS later_table;")


async def test_a_conflict_that_is_not_the_fingerprint_is_reported_where_it_happens(
    db, register
):
    """An untargeted ON CONFLICT would surface this as a missing row.

    The insert absorbs a conflict on (relationship_id, input_fingerprint) so a
    replay converges. Any OTHER unique violation is a real defect and has to
    arrive as the integrity error it is, not as `NoResultFound` from a
    re-select that then finds nothing.
    """
    from sqlalchemy.exc import IntegrityError

    from sonder.domain.schemas.preference import PreferenceModel
    from sonder.storage.db import dyad_session, maintenance_session
    from sonder.storage.repositories import PreferenceRepo
    from sonder.storage.scope import Scope

    user_id, person_id, rel_id = uuid4(), uuid4(), uuid4()
    async with maintenance_session() as s:
        await _seed(s, user_id, person_id, rel_id, "Versioned")
        await s.execute(
            text(
                "INSERT INTO preference_models (person_id,relationship_id,version,"
                "scorer_version,input_fingerprint) VALUES (:p,:r,1,'v','fp-a')"
            ),
            {"p": person_id, "r": rel_id},
        )
    claim = await register(user_id)

    # Same relationship, same version, different inputs: collides on
    # (relationship_id, version), which is not the fingerprint index.
    model = PreferenceModel(
        person_id=person_id,
        relationship_id=rel_id,
        version=1,
        entries={},
        scorer_version="v",
    )
    scope = Scope(user_id, rel_id, claim)
    with pytest.raises(IntegrityError):
        async with dyad_session(scope) as s:
            await PreferenceRepo(s).save_model(scope, model, input_fingerprint="fp-b")


# ------------------------------------------------------------------ the vault


async def test_vault_relationship_must_belong_to_its_user(db):
    from sqlalchemy.exc import IntegrityError

    from sonder.storage.db import maintenance_session
    from sonder.storage.repositories import ScopeViolation, VaultRepo
    from sonder.storage.scope import UserScope

    owner, other = uuid4(), uuid4()
    owner_relationship, other_relationship = uuid4(), uuid4()
    async with maintenance_session() as s:
        await _seed(s, owner, uuid4(), owner_relationship, "Owner")
        await _seed(s, other, uuid4(), other_relationship, "Other")

    async with maintenance_session() as s:
        vault_id = await VaultRepo(s).add(
            UserScope(owner),
            relationship_id=owner_relationship,
            kind="statement",
            ciphertext=b"encrypted",
            key_version="v1",
            provenance="user_stated",
        )
        assert vault_id is not None

    with pytest.raises(ScopeViolation, match="does not belong"):
        async with maintenance_session() as s:
            await VaultRepo(s).add(
                UserScope(owner),
                relationship_id=other_relationship,
                kind="statement",
                ciphertext=b"encrypted",
                key_version="v1",
                provenance="user_stated",
            )

    with pytest.raises(IntegrityError):
        async with maintenance_session() as s:
            await s.execute(
                text(
                    "INSERT INTO vault_records "
                    "(user_id,relationship_id,kind,ciphertext,key_version,provenance) "
                    "VALUES (:u,:r,'statement',:c,'v1','user_stated')"
                ),
                {"u": owner, "r": other_relationship, "c": b"encrypted"},
            )


async def test_integrity_and_vault_migrations_are_rerunnable_without_duplicates(
    db, superuser
):
    from pathlib import Path

    from sonder.storage.db import maintenance_session

    migrations = Path(__file__).parents[1] / "migrations"
    for name in (
        "004_integrity_and_idempotency.sql",
        "006_vault_ownership_and_cleanup.sql",
        "004_integrity_and_idempotency.sql",
        "006_vault_ownership_and_cleanup.sql",
    ):
        await superuser((migrations / name).read_text())

    async with maintenance_session() as s:
        duplicates = (
            await s.execute(
                text(
                    "SELECT tgrelid::regclass::text, count(*) "
                    "FROM pg_trigger "
                    "WHERE NOT tgisinternal "
                    "AND tgname LIKE '%\\_scoped\\_references%' ESCAPE '\\' "
                    "GROUP BY tgrelid HAVING count(*) > 1"
                )
            )
        ).all()
        duplicate_constraints = (
            (
                await s.execute(
                    text(
                        "SELECT conname FROM pg_constraint "
                        "WHERE conname LIKE '%\\_upgrade' ESCAPE '\\' "
                        "OR conname LIKE '%\\_integrity' ESCAPE '\\'"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert duplicates == []
    assert duplicate_constraints == []


async def test_vault_upgrade_fails_closed_on_legacy_cross_owner_rows(db, superuser):
    from pathlib import Path

    from asyncpg.exceptions import CheckViolationError

    from sonder.storage.db import maintenance_session

    owner, other = uuid4(), uuid4()
    owner_relationship, other_relationship = uuid4(), uuid4()
    bad_vault = uuid4()
    async with maintenance_session() as s:
        await _seed(s, owner, uuid4(), owner_relationship, "Legacy owner")
        await _seed(s, other, uuid4(), other_relationship, "Legacy other")

    # Recreate the legacy shape: relationship existence was checked, ownership
    # was not. This is intentionally separate from the failing migration so
    # its preflight sees a committed legacy row.
    await superuser(
        "ALTER TABLE vault_records DROP CONSTRAINT "
        "vault_records_relationship_same_user; "
        "ALTER TABLE vault_records ADD CONSTRAINT "
        "vault_records_relationship_id_fkey FOREIGN KEY (relationship_id) "
        "REFERENCES relationships(id) ON DELETE CASCADE; "
        "INSERT INTO vault_records "
        "(id,user_id,relationship_id,kind,ciphertext,key_version,provenance) "
        f"VALUES ('{bad_vault}','{owner}','{other_relationship}',"
        "'statement','\\x00','v1','user_stated');"
    )

    migration = (
        Path(__file__).parents[1] / "migrations" / "006_vault_ownership_and_cleanup.sql"
    ).read_text()
    with pytest.raises(CheckViolationError, match="vault ownership preflight"):
        await superuser(migration)

    # Restore the upgraded shape so this regression test is isolated from the
    # remainder of the real-Postgres suite.
    await superuser(f"DELETE FROM vault_records WHERE id = '{bad_vault}';")
    await superuser(migration)


async def test_role_script_rotates_quoted_passwords_and_the_roles_can_log_in(db):
    import shutil
    import subprocess
    from pathlib import Path

    from sqlalchemy.engine import make_url

    superuser_url = os.environ.get("SONDER_SUPERUSER_DATABASE_URL")
    if superuser_url is None or shutil.which("psql") is None:
        pytest.skip("superuser DSN and host psql are required")

    role_variables = {
        "sonder_app": ("SONDER_APP_DB_PASSWORD", "SONDER_TEST_APP_DB_PASSWORD"),
        "sonder_identity": (
            "SONDER_IDENTITY_DB_PASSWORD",
            "SONDER_TEST_IDENTITY_DB_PASSWORD",
        ),
        "sonder_erase": (
            "SONDER_ERASURE_DB_PASSWORD",
            "SONDER_TEST_ERASURE_DB_PASSWORD",
        ),
        "sonder_worker": (
            "SONDER_WORKER_DB_PASSWORD",
            "SONDER_TEST_WORKER_DB_PASSWORD",
        ),
        "sonder_maint": (
            "SONDER_MAINT_DB_PASSWORD",
            "SONDER_TEST_MAINT_DB_PASSWORD",
        ),
        "sonder_bot": ("SONDER_BOT_DB_PASSWORD", "SONDER_TEST_BOT_DB_PASSWORD"),
    }
    original = {
        script_var: os.environ[test_var]
        for script_var, test_var in role_variables.values()
    }
    rotated = {
        script_var: f"quote'!@#$%^&*()[]{{}}:;,.?/{index}"
        for index, (script_var, _) in enumerate(role_variables.values())
    }
    url = make_url(superuser_url)
    connection_env = {
        "PGHOST": url.host or "127.0.0.1",
        "PGPORT": str(url.port or 5432),
        "PGDATABASE": url.database or "postgres",
        "PGUSER": url.username or "postgres",
        "PGPASSWORD": url.password or "",
    }
    script = Path(__file__).parents[1] / "scripts" / "db-roles.sh"

    def run_script(passwords):
        subprocess.run(
            [str(script)],
            env={**os.environ, **connection_env, **passwords},
            check=True,
            capture_output=True,
            text=True,
        )

    try:
        run_script(rotated)
        for role, (script_var, _) in role_variables.items():
            result = subprocess.run(
                [
                    "psql",
                    "--no-psqlrc",
                    "--quiet",
                    "--tuples-only",
                    "--command=SELECT current_user",
                ],
                env={
                    **os.environ,
                    **connection_env,
                    "PGUSER": role,
                    "PGPASSWORD": rotated[script_var],
                },
                check=True,
                capture_output=True,
                text=True,
            )
            assert result.stdout.strip() == role
    finally:
        run_script(original)


async def test_the_request_role_cannot_reach_the_vault_at_all(db, register):
    """The vault is the most sensitive table here, and the answer is blunt.

    It holds what someone actually said about their own body, encrypted, and
    every row cites a statement rather than an inference. Rather than scoping
    the request role's view of it, the request role has no privilege on the
    table -- which is a stronger guarantee than any policy, because it stops
    being a question about which rows.
    """
    from sqlalchemy.exc import ProgrammingError

    from sonder.storage.db import dyad_session, maintenance_session
    from sonder.storage.scope import Scope

    user_id, person_id, rel_id = uuid4(), uuid4(), uuid4()
    async with maintenance_session() as s:
        await _seed(s, user_id, person_id, rel_id, "Vaulted")
    claim = await register(user_id)

    with pytest.raises(ProgrammingError, match="permission denied"):
        async with dyad_session(Scope(user_id, rel_id, claim)) as s:
            await s.execute(text("SELECT count(*) FROM vault_records"))


async def test_no_role_holds_a_vault_privilege_without_a_policy(db):
    """A privilege with no policy is a brace with no belt.

    It reads nothing today, because row security denies by default. It starts
    reading the moment someone adds a policy for that role, and whoever adds
    it will reasonably assume the grant was deliberate.
    """
    from sonder.storage.db import maintenance_session

    async with maintenance_session() as s:
        privileged = {
            row[0]
            for row in (
                await s.execute(
                    text(
                        "SELECT rolname FROM pg_roles "
                        " WHERE rolname LIKE 'sonder\\_%' "
                        "   AND NOT rolbypassrls "
                        "   AND has_table_privilege("
                        "         rolname, 'vault_records', 'SELECT')"
                    )
                )
            ).all()
        }
        with_policy = {
            role
            for row in (
                await s.execute(
                    text(
                        "SELECT unnest(roles)::text FROM pg_policies "
                        " WHERE tablename = 'vault_records'"
                    )
                )
            ).all()
            for role in [row[0]]
        }
    assert privileged - with_policy == set(), (
        f"these roles can reach vault_records with no policy: {privileged - with_policy}"
    )


async def test_one_person_can_be_the_target_of_two_relationships(db, register):
    """`UNIQUE (person_id, version)` made the second one fail on a hidden row."""
    from sonder.domain.schemas.preference import PreferenceModel
    from sonder.storage.db import dyad_session, maintenance_session
    from sonder.storage.repositories import PreferenceRepo
    from sonder.storage.scope import Scope

    user_id, person_id = uuid4(), uuid4()
    rel_a, rel_b = uuid4(), uuid4()
    async with maintenance_session() as s:
        await _seed(s, user_id, person_id, rel_a, "Shared")
        await s.execute(
            text(
                "INSERT INTO relationships (id,user_id,target_person_id) "
                "VALUES (:r,:u,:p)"
            ),
            {"r": rel_b, "u": user_id, "p": person_id},
        )
    claim = await register(user_id)

    # Both relationships number their first preference model version 1.
    for rel, fingerprint in ((rel_a, "fp-a"), (rel_b, "fp-b")):
        model = PreferenceModel(
            person_id=person_id,
            relationship_id=rel,
            version=1,
            entries={},
            scorer_version="v",
        )
        scope = Scope(user_id, rel, claim)
        async with dyad_session(scope) as s:
            await PreferenceRepo(s).save_model(
                scope, model, input_fingerprint=fingerprint
            )

    async with maintenance_session() as s:
        count = (
            await s.execute(
                text(
                    "SELECT count(*) FROM preference_models "
                    " WHERE relationship_id IN (:a, :b)"
                ),
                {"a": rel_a, "b": rel_b},
            )
        ).scalar()
    assert count == 2, "the second relationship could not version from 1"
