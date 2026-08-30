"""Authentication.

Bearer tokens, stored as SHA-256 digests. Multi-user from the start, because
retrofitting a user boundary onto code that assumed one account is how data
from one account ends up visible to another.

Three rules the rest of the API depends on:
  * `current_identity` is the ONLY source of identity; no route reads a user
    id from the request body
  * `owned_relationship` / `owned_person` verify the object belongs to the
    caller before any scoped work begins
  * neither of them holds a credential that can read across accounts

That last rule is the one that changed. Authentication used to run on the
BYPASSRLS maintenance role, which meant every request path carried a
whole-database credential for the sake of one token lookup. It now runs on
`sonder_identity`, a role with no table privileges at all: it can execute two
authentication RPCs and nothing else. The ownership checks below then run as
the ordinary app role, where row-level security -- not this code -- is what
stops them seeing another account.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from uuid import UUID

from fastapi import Depends, Header, HTTPException
from sqlalchemy import select, text

from ..storage.db import bind_identity, identity_session, user_session
from ..storage.models import PersonProfileRow, RelationshipRow
from ..storage.scope import UserScope


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def mint_token() -> tuple[str, str]:
    """Returns (plaintext, digest). The plaintext is shown once and not kept."""
    token = "sk-sonder-" + secrets.token_urlsafe(32)
    return token, hash_token(token)


@dataclass(frozen=True, slots=True)
class Identity:
    """An authenticated caller, and the claim that proves it to Postgres."""

    user_id: UUID
    claim: str

    def __str__(self) -> str:  # the claim is a live credential; never log it
        return f"Identity(u={str(self.user_id)[:8]})"


async def authenticate(digest: str) -> Identity | None:
    """Exchange a token digest for an identity, over the one RPC.

    Returns None for a token that does not exist and for one that has been
    revoked. The caller cannot distinguish the two, which is deliberate.
    """
    async with identity_session() as session:
        row = (
            await session.execute(
                text("SELECT user_id, identity_claim FROM auth_bearer(:digest)"),
                {"digest": digest},
            )
        ).first()
    if row is None:
        return None
    return Identity(user_id=row.user_id, claim=row.identity_claim)


async def current_identity(
    authorization: str | None = Header(default=None),
) -> Identity:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "missing bearer token")
    identity = await authenticate(hash_token(authorization.split(None, 1)[1].strip()))
    if identity is None:
        raise HTTPException(401, "invalid or revoked token")
    # Bound for the rest of the request, so a Scope built downstream carries
    # the claim without every call site having to thread it through.
    bind_identity(identity.user_id, identity.claim)
    return identity


async def current_user_id(identity: Identity = Depends(current_identity)) -> UUID:
    return identity.user_id


async def owned_relationship(
    relationship_id: UUID, identity: Identity = Depends(current_identity)
) -> UUID:
    """Ownership check before any dyad-scoped work.

    RLS scopes a session to one relationship; it does not say whose. Without
    this check, knowing a relationship id would be enough to reach the API for
    it -- and the caller would get a 500 from the database rather than a 404.
    The query runs under the user-scoped policy, so it can only ever see the
    caller's own rows.
    """
    scope = UserScope(user_id=identity.user_id, identity_claim=identity.claim)
    async with user_session(scope) as session:
        owner = (
            await session.execute(
                select(RelationshipRow.id).where(RelationshipRow.id == relationship_id)
            )
        ).scalar()
    if owner is None:
        # 404, not 403: whether someone else's relationship exists is itself
        # something this endpoint should not disclose.
        raise HTTPException(404, "no such relationship")
    return relationship_id


async def owned_person(
    person_id: UUID, identity: Identity = Depends(current_identity)
) -> UUID:
    scope = UserScope(user_id=identity.user_id, identity_claim=identity.claim)
    async with user_session(scope) as session:
        owner = (
            await session.execute(
                select(PersonProfileRow.id).where(PersonProfileRow.id == person_id)
            )
        ).scalar()
    if owner is None:
        raise HTTPException(404, "no such person")
    return person_id
