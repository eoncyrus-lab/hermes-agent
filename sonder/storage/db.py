"""Engine and the scoped session.

`dyad_session` is the only sanctioned way to touch dyad-scoped data. It sets
`app.relationship_id` for the transaction, which activates the RLS policies
in migrations/001_core.sql. A query that forgets its WHERE clause returns
zero rows instead of returning another person's.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from ..config import settings
from .scope import Scope, UserScope

_engine = None
_factory: async_sessionmaker[AsyncSession] | None = None
_maint_engine = None
_maint_factory: async_sessionmaker[AsyncSession] | None = None
_identity_engine = None
_identity_factory: async_sessionmaker[AsyncSession] | None = None
_erasure_engine = None
_erasure_factory: async_sessionmaker[AsyncSession] | None = None
_worker_engine = None
_worker_factory: async_sessionmaker[AsyncSession] | None = None

# Authentication happens on a table-blind database role.  The signed claim it
# returns is request-local and is verified again by PostgreSQL RLS; setting a
# user-id GUC by itself therefore grants nothing.
_identity_context: ContextVar[tuple[str, str] | None] = ContextVar(
    "sonder_identity", default=None
)


def bind_identity(user_id: object, claim: str) -> None:
    _identity_context.set((str(user_id), claim))


def _claim_for(user_id: object, explicit: str | None) -> str:
    if explicit:
        return explicit
    current = _identity_context.get()
    if current is not None and current[0] == str(user_id):
        return current[1]
    raise RuntimeError("an authenticated database identity is required")


def engine():
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            settings().database_url, pool_pre_ping=True, pool_size=10
        )
    return _engine


def session_factory() -> async_sessionmaker[AsyncSession]:
    global _factory
    if _factory is None:
        _factory = async_sessionmaker(engine(), expire_on_commit=False)
    return _factory


def _required(url: str | None, variable: str) -> str:
    """Fail closed. A missing role DSN degrades silently, not loudly.

    Falling back to the app role looks like it works: the worker builds
    preference models with empty cross-relationship priors because
    `user_tactic_priors` has no policy for it, outcome scoring sees zero
    relationships forever, and erasure reports every count as zero while the
    cascade quietly removes the data. Every one of those is a wrong answer
    delivered confidently, so refuse to start instead.
    """
    if not url:
        raise RuntimeError(f"{variable} is required")
    return url


def maintenance_factory() -> async_sessionmaker[AsyncSession]:
    global _maint_engine, _maint_factory
    if _maint_factory is None:
        url = _required(
            settings().maintenance_database_url, "SONDER_MAINTENANCE_DATABASE_URL"
        )
        _maint_engine = create_async_engine(url, pool_pre_ping=True, pool_size=3)
        _maint_factory = async_sessionmaker(_maint_engine, expire_on_commit=False)
    return _maint_factory


def identity_factory() -> async_sessionmaker[AsyncSession]:
    """Factory for the RPC-only identity role (never a business-data role)."""
    global _identity_engine, _identity_factory
    if _identity_factory is None:
        url = settings().identity_database_url
        if not url:
            raise RuntimeError("SONDER_IDENTITY_DATABASE_URL is required")
        _identity_engine = create_async_engine(url, pool_pre_ping=True, pool_size=5)
        _identity_factory = async_sessionmaker(_identity_engine, expire_on_commit=False)
    return _identity_factory


def erasure_factory() -> async_sessionmaker[AsyncSession]:
    """Factory for the user-scoped erasure role.

    It reads one user's rows across every relationship, which is exactly the
    capability erasure needs and nothing else needs.
    """
    global _erasure_engine, _erasure_factory
    if _erasure_factory is None:
        url = _required(settings().erasure_database_url, "SONDER_ERASURE_DATABASE_URL")
        _erasure_engine = create_async_engine(url, pool_pre_ping=True, pool_size=3)
        _erasure_factory = async_sessionmaker(_erasure_engine, expire_on_commit=False)
    return _erasure_factory


def worker_factory() -> async_sessionmaker[AsyncSession]:
    """Factory for the pipeline role."""
    global _worker_engine, _worker_factory
    if _worker_factory is None:
        url = _required(settings().worker_database_url, "SONDER_WORKER_DATABASE_URL")
        _worker_engine = create_async_engine(url, pool_pre_ping=True, pool_size=5)
        _worker_factory = async_sessionmaker(_worker_engine, expire_on_commit=False)
    return _worker_factory


async def _set_identity(s: AsyncSession, user_id: object, claim: str) -> None:
    await s.execute(
        text(
            "SELECT set_config('app.user_id', :uid, true), "
            "set_config('app.identity_claim', :claim, true)"
        ),
        {"uid": str(user_id), "claim": claim},
    )


@asynccontextmanager
async def identity_session() -> AsyncIterator[AsyncSession]:
    """Execute only the authentication RPCs granted to ``sonder_identity``."""
    async with identity_factory()() as s:
        try:
            yield s
            await s.commit()
        except Exception:
            await s.rollback()
            raise


@asynccontextmanager
async def dyad_session(scope: Scope) -> AsyncIterator[AsyncSession]:
    """A transaction pinned to exactly one relationship."""
    async with session_factory()() as s:
        claim = _claim_for(scope.user_id, scope.identity_claim)
        await _set_identity(s, scope.user_id, claim)
        await s.execute(
            text("SELECT set_config('app.relationship_id', :rid, true)"),
            {"rid": str(scope.relationship_id)},
        )
        try:
            yield s
            await s.commit()
        except Exception:
            await s.rollback()
            raise


@asynccontextmanager
async def user_session(scope: UserScope) -> AsyncIterator[AsyncSession]:
    """User-scoped data only. RLS on dyad tables denies everything here."""
    async with session_factory()() as s:
        claim = _claim_for(scope.user_id, scope.identity_claim)
        await _set_identity(s, scope.user_id, claim)
        await s.execute(text("SELECT set_config('app.relationship_id', '', true)"))
        try:
            yield s
            await s.commit()
        except Exception:
            await s.rollback()
            raise


@asynccontextmanager
async def erasure_session(scope: UserScope) -> AsyncIterator[AsyncSession]:
    """Every relationship one user has, and nothing beyond that user.

    Erasure and inspection are the only request paths that legitimately span
    relationships. They used to run on the BYPASSRLS maintenance role, which
    put a whole-database credential on the request path for the sake of two
    endpoints. This role is scoped by the same verified identity claim as
    every other session; the widening is to the user's own relationships only.
    """
    async with erasure_factory()() as s:
        claim = _claim_for(scope.user_id, scope.identity_claim)
        await _set_identity(s, scope.user_id, claim)
        await s.execute(text("SELECT set_config('app.relationship_id', '', true)"))
        try:
            yield s
            await s.commit()
        except Exception:
            await s.rollback()
            raise


@asynccontextmanager
async def worker_session(scope: Scope) -> AsyncIterator[AsyncSession]:
    """A queued job's transaction, pinned to one relationship.

    A job arrives from a queue with no bearer token, so there is no identity
    claim to verify and the database can only enforce the pinned relationship.
    The other half -- that the job's user id actually owns that relationship
    -- has to be checked in the job itself, before any work: see
    `worker.tasks.authorize`.
    """
    async with worker_factory()() as s:
        await s.execute(
            text("SELECT set_config('app.relationship_id', :rid, true)"),
            {"rid": str(scope.relationship_id)},
        )
        try:
            yield s
            await s.commit()
        except Exception:
            await s.rollback()
            raise


@asynccontextmanager
async def maintenance_session() -> AsyncIterator[AsyncSession]:
    """The few operations that legitimately span relationships.

    Outcome scoring and artifact purging. User erasure and inspection use the
    user-scoped ``sonder_erase`` role instead. This session runs as a separate
    database role which in production is the only one with BYPASSRLS -- so
    reaching for it by mistake in a request handler is a deployment-visible
    privilege, not a silent one.

    Callers are responsible for their own ownership checks: RLS is not
    covering them here.
    """
    async with maintenance_factory()() as s:
        try:
            yield s
            await s.commit()
        except Exception:
            await s.rollback()
            raise


# Retained name for the maintenance path; every caller must have done its own
# ownership check before reaching for it.
admin_session = maintenance_session
