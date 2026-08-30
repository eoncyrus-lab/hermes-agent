"""The scope object.

Every read and write of dyad-scoped data goes through one of these. It is a
required argument, not an optional filter, so "forgetting the WHERE clause"
stops being an available mistake at the type level -- and Postgres RLS
(migrations/001_core.sql) catches whatever still slips through.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class Scope:
    """Authorises access to exactly one relationship."""

    user_id: UUID
    relationship_id: UUID
    identity_claim: str | None = None

    def __str__(self) -> str:  # never log the ids in full
        return f"Scope(u={str(self.user_id)[:8]}, r={str(self.relationship_id)[:8]})"


@dataclass(frozen=True, slots=True)
class UserScope:
    """Authorises access to user-level data only.

    Voice profile, self profile, tactic priors. Deliberately cannot reach any
    dyad-scoped table -- there is no relationship_id on it to reach with.
    """

    user_id: UUID
    identity_claim: str | None = None

    def __str__(self) -> str:
        return f"UserScope(u={str(self.user_id)[:8]})"
