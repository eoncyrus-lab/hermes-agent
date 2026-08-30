"""Shared fixtures.

The engines are module-level singletons, which is right for a process that
serves requests and wrong for a test suite where pytest-asyncio gives each
test its own event loop. Left alone, the second database test inherits a
connection pool bound to a closed loop. So each test builds and disposes its
own engines.
"""

from __future__ import annotations

import hashlib
import os
from uuid import UUID

import pytest
from sqlalchemy import text

DB_URL = os.environ.get("SONDER_TEST_DATABASE_URL")
MAINT_URL = os.environ.get("SONDER_MAINTENANCE_DATABASE_URL") or DB_URL
ERASE_URL = os.environ.get("SONDER_ERASURE_DATABASE_URL") or DB_URL
WORKER_URL = os.environ.get("SONDER_WORKER_DATABASE_URL") or DB_URL
IDENTITY_URL = os.environ.get("SONDER_IDENTITY_DATABASE_URL")
# Superuser DSN, for the few tests that replay a migration to prove it is
# rerunnable in any order without giving a privilege back.
SUPERUSER_URL = os.environ.get("SONDER_SUPERUSER_DATABASE_URL")


@pytest.fixture(autouse=True)
def _ignore_dotenv(monkeypatch):
    """Keep a developer's real `.env` out of the suite.

    `Settings` reads `.env`, so whoever has the app actually configured runs a
    different test suite from everyone else: a local OpenRouter key and route
    chain turned twelve provider tests red while the code was fine. Tests that
    care about configuration build `Settings(...)` explicitly or set env vars,
    so pointing the env_file at a path that does not exist costs nothing and
    makes the run reproducible.
    """
    import sonder.config as config

    monkeypatch.setitem(config.Settings.model_config, "env_file", None)
    config.settings.cache_clear()
    yield
    config.settings.cache_clear()


_ENGINES = (
    "_engine",
    "_maint_engine",
    "_erasure_engine",
    "_worker_engine",
    "_identity_engine",
)
_FACTORIES = (
    ("_engine", "_factory"),
    ("_maint_engine", "_maint_factory"),
    ("_erasure_engine", "_erasure_factory"),
    ("_worker_engine", "_worker_factory"),
    ("_identity_engine", "_identity_factory"),
)


def _reset(dbmod) -> None:
    for engine_attr, factory_attr in _FACTORIES:
        setattr(dbmod, engine_attr, None)
        setattr(dbmod, factory_attr, None)


@pytest.fixture
async def db(monkeypatch):
    """A database bound to this test's event loop."""
    if not DB_URL:
        pytest.skip("SONDER_TEST_DATABASE_URL not set")

    monkeypatch.setenv("SONDER_DATABASE_URL", DB_URL)
    monkeypatch.setenv("SONDER_MAINTENANCE_DATABASE_URL", MAINT_URL)
    monkeypatch.setenv("SONDER_ERASURE_DATABASE_URL", ERASE_URL)
    monkeypatch.setenv("SONDER_WORKER_DATABASE_URL", WORKER_URL)
    if IDENTITY_URL:
        monkeypatch.setenv("SONDER_IDENTITY_DATABASE_URL", IDENTITY_URL)

    import sonder.config as config
    import sonder.storage.db as dbmod

    config.settings.cache_clear()
    _reset(dbmod)

    yield dbmod

    for attr in _ENGINES:
        eng = getattr(dbmod, attr, None)
        if eng is not None:
            await eng.dispose()
    _reset(dbmod)
    config.settings.cache_clear()


@pytest.fixture
def register(db):
    """Give a user id a live bearer token, and bind its claim for this test.

    Every scoped session presents the short-lived HMAC returned by the
    table-blind identity RPC. A raw digest or invented user id proves nothing.
    """

    async def _register(user_id: UUID) -> str:
        digest = hashlib.sha256(f"test-token:{user_id}".encode()).hexdigest()
        async with db.maintenance_session() as session:
            await session.execute(
                text("INSERT INTO users (id) VALUES (:u) ON CONFLICT DO NOTHING"),
                {"u": user_id},
            )
            await session.execute(
                text(
                    "INSERT INTO api_tokens (user_id, token_sha256) "
                    "VALUES (:u, :d) ON CONFLICT DO NOTHING"
                ),
                {"u": user_id, "d": digest},
            )
        async with db.identity_session() as session:
            row = (
                await session.execute(
                    text("SELECT identity_claim FROM auth_bearer(:digest)"),
                    {"digest": digest},
                )
            ).first()
        assert row is not None
        claim = row.identity_claim
        db.bind_identity(user_id, claim)
        return claim

    return _register


@pytest.fixture
def erasure(db, register):
    """An erasure-role session for one registered user."""

    def _open(user_id: UUID):
        from sonder.storage.scope import UserScope

        return db.erasure_session(UserScope(user_id=user_id))

    return _open


@pytest.fixture
def superuser():
    """Run a whole migration file as the migration identity, or skip.

    Goes through asyncpg's simple-query protocol, which executes a
    multi-statement script the way psql does -- so dollar-quoted function
    bodies and DO blocks survive, and no SQL splitting is needed here.
    """
    if not SUPERUSER_URL:
        pytest.skip("SONDER_SUPERUSER_DATABASE_URL not set")

    from sqlalchemy.ext.asyncio import create_async_engine

    async def _run(sql: str):
        engine = create_async_engine(SUPERUSER_URL)
        try:
            async with engine.connect() as conn:
                raw = await conn.get_raw_connection()
                await raw.driver_connection.execute(sql)
        finally:
            await engine.dispose()

    return _run
