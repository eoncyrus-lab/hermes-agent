"""Erasure.

For a personal-use deployment this is the control that makes modelling
someone acceptable: whatever the system has inferred about a person can be
removed completely, on demand, by the person running it.

So it has to be complete, and it has to be checkable. Deleting the
relationship alone is not enough -- that leaves `person_profiles` behind,
and the inferred profile is precisely the thing worth being able to erase.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC
from uuid import UUID

import structlog
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from .models import (
    ConsentRecordRow,
    InteractionRow,
    MemoryChunkRow,
    MessageRow,
    PersonProfileRow,
    PreferenceEvidenceRow,
    PreferenceModelRow,
    RelationshipRow,
    RelationshipStateRow,
    SignalRow,
    StrategyOutcomeRow,
    StrategyRecordRow,
    VoiceProfileRow,
)

log = structlog.get_logger(__name__)

# Everything that can hold something about one person. Kept as an explicit
# list rather than relying on cascades so that a new table added later
# without a cascade shows up as a failing test instead of as residue.
DYAD_TABLES = (
    ("messages", MessageRow),
    ("signals", SignalRow),
    ("interactions", InteractionRow),
    ("relationship_states", RelationshipStateRow),
    ("preference_evidence", PreferenceEvidenceRow),
    ("preference_models", PreferenceModelRow),
    ("strategy_outcomes", StrategyOutcomeRow),
    ("strategy_records", StrategyRecordRow),
    ("memory_chunks", MemoryChunkRow),
    ("consent_records", ConsentRecordRow),
)


@dataclass
class Receipt:
    """What was actually removed. Returned to the user, not just logged."""

    person_id: UUID | None = None
    display_name: str | None = None
    relationships: int = 0
    rows: dict[str, int] = field(default_factory=dict)
    residue: dict[str, int] = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        return not any(self.residue.values())

    def summary(self) -> str:
        total = sum(self.rows.values())
        head = (
            f"Erased {self.display_name or self.person_id}: "
            f"{total} rows across {len(self.rows)} tables, "
            f"{self.relationships} relationship(s)."
        )
        if self.complete:
            return head + " Nothing remains."
        left = ", ".join(f"{k}={v}" for k, v in self.residue.items() if v)
        return head + f" WARNING -- residue remains: {left}"


async def _count_for_relationships(
    session: AsyncSession, rel_ids: list[UUID]
) -> dict[str, int]:
    if not rel_ids:
        return {}
    out: dict[str, int] = {}
    for name, model in DYAD_TABLES:
        if name == "messages":
            stmt = (
                select(func.count())
                .select_from(MessageRow)
                .join(InteractionRow, MessageRow.interaction_id == InteractionRow.id)
                .where(InteractionRow.relationship_id.in_(rel_ids))
            )
        else:
            stmt = (
                select(func.count())
                .select_from(model)
                .where(model.relationship_id.in_(rel_ids))
            )
        out[name] = (await session.execute(stmt)).scalar() or 0
    return out


async def profile_versions(session: AsyncSession, person_id: UUID) -> list[UUID]:
    """Every version of one person's profile, not just the current row.

    `person_profiles` is versioned: each row keeps its own `attributes` and
    `traits`, and `supersedes` chains them. Erasing only the row whose id was
    passed would leave every earlier inference about that person on disk while
    the receipt said "complete" -- the exact failure erasure exists to prevent.

    The walk goes both ways along the chain, so it does not matter which
    version the caller happens to hold. It runs under the caller's own row
    security, so it can only ever return this user's rows.
    """
    rows = (
        await session.execute(
            text(
                """
                WITH RECURSIVE chain(id, supersedes) AS (
                    SELECT id, supersedes FROM person_profiles WHERE id = :pid
                  UNION
                    SELECT p.id, p.supersedes
                      FROM person_profiles p
                      JOIN chain c
                        ON p.id = c.supersedes OR p.supersedes = c.id
                )
                SELECT id FROM chain
                """
            ),
            {"pid": person_id},
        )
    ).scalars()
    return list(rows)


async def inspect_person(session: AsyncSession, person_id: UUID) -> dict:
    """Everything held about one person, so erasure is an informed choice."""
    profile = await session.get(PersonProfileRow, person_id)
    if profile is None:
        return {"found": False}

    rel_ids = list(
        (
            await session.execute(
                select(RelationshipRow.id).where(
                    RelationshipRow.target_person_id == person_id
                )
            )
        ).scalars()
    )
    counts = await _count_for_relationships(session, rel_ids)
    return {
        "found": True,
        "person_id": str(person_id),
        "display_name": profile.display_name,
        "data_subject": profile.data_subject,
        "retention_until": profile.retention_until.isoformat()
        if profile.retention_until
        else None,
        "inferred_attributes": sorted(profile.attributes or {}),
        "inferred_traits": sorted(profile.traits or {}),
        "relationships": len(rel_ids),
        "stored_rows": counts,
        "total_rows": sum(counts.values()),
    }


async def forget_person(session: AsyncSession, person_id: UUID) -> Receipt:
    """Erase a person and everything derived from them.

    Runs in one transaction and verifies afterwards: a deletion that quietly
    leaves an inferred profile behind is worse than no deletion, because the
    user believes it worked.
    """
    profile = await session.get(PersonProfileRow, person_id)
    receipt = Receipt(person_id=person_id)
    if profile is None:
        return receipt
    receipt.display_name = profile.display_name

    version_ids = await profile_versions(session, person_id)
    rel_ids = list(
        (
            await session.execute(
                select(RelationshipRow.id).where(
                    RelationshipRow.target_person_id.in_(version_ids)
                )
            )
        ).scalars()
    )
    receipt.relationships = len(rel_ids)
    receipt.rows = await _count_for_relationships(session, rel_ids)

    # Relationship rows cascade to every dyad-scoped table. The profile and
    # its voice rows do not hang off a relationship, so they need saying.
    if rel_ids:
        await session.execute(
            delete(RelationshipRow).where(RelationshipRow.id.in_(rel_ids))
        )
    await session.execute(
        delete(VoiceProfileRow).where(VoiceProfileRow.person_id.in_(version_ids))
    )
    # Every version, not just the one that was asked for.
    await session.execute(
        delete(PersonProfileRow).where(PersonProfileRow.id.in_(version_ids))
    )
    await session.flush()

    receipt.residue = await verify_erased(session, person_id, version_ids)
    if not receipt.complete:
        log.error("erasure_incomplete", residue=receipt.residue)
    else:
        log.info("person_erased", rows=sum(receipt.rows.values()))
    return receipt


async def verify_erased(
    session: AsyncSession, person_id: UUID, version_ids: list[UUID]
) -> dict[str, int]:
    """Post-deletion sweep. Counts anything that should no longer exist.

    Two counts, and both are real: `person_profiles` and `relationships`
    resolve through `app_current_user_id()`, which still answers after the
    delete.

    Everything else is deliberately not counted here, and that is a limit
    worth stating rather than papering over. The erasure role reaches dyad
    rows through `relationship_id IN (SELECT id FROM relationships)` and
    `voice_profiles` through `person_id IN (SELECT id FROM person_profiles)`
    -- both of those parents are gone by this point, so such a count returns
    zero whether or not the rows survived. A sweep that always passes is worse
    than no sweep, because it reads like evidence.

    What that sweep was actually defending against -- a dyad table that does
    not cascade from `relationships` -- is a property of the schema, not of
    any one erasure. It is asserted once, against a real database, in
    `tests/test_erasure.py::test_every_dyad_table_cascades_from_relationships`.
    """
    residue: dict[str, int] = {}

    ids = version_ids or [person_id]
    residue["person_profiles"] = (
        await session.execute(
            select(func.count())
            .select_from(PersonProfileRow)
            .where(PersonProfileRow.id.in_(ids))
        )
    ).scalar() or 0
    residue["relationships"] = (
        await session.execute(
            select(func.count())
            .select_from(RelationshipRow)
            .where(RelationshipRow.target_person_id.in_(ids))
        )
    ).scalar() or 0

    return residue


async def orphaned_profiles(session: AsyncSession, user_id: UUID) -> list[dict]:
    """Third-party profiles with no relationship left, or past retention.

    Residue accumulates from partial deletions and from relationships that
    simply ended. Surfaced so it can be cleared rather than sitting there.
    """
    rows = (
        await session.execute(
            text(
                """
                SELECT p.id, p.display_name, p.retention_until,
                       (SELECT count(*) FROM relationships r
                         WHERE r.target_person_id = p.id) AS rel_count
                  FROM person_profiles p
                 WHERE p.user_id = :u
                   AND p.data_subject = 'third_party'
                """
            ),
            {"u": user_id},
        )
    ).mappings()
    from datetime import datetime

    now = datetime.now(UTC)
    out = []
    for r in rows:
        expired = r["retention_until"] is not None and r["retention_until"] < now
        if r["rel_count"] == 0 or expired:
            out.append(
                {
                    "person_id": str(r["id"]),
                    "display_name": r["display_name"],
                    "reason": "no relationship" if r["rel_count"] == 0 else "expired",
                }
            )
    return out
