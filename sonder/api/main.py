"""HTTP API.

Identity comes from `current_identity` and nowhere else; no route reads a
user id from a request body. Any route naming a relationship or a person passes
through an ownership check first, because RLS scopes a session to one
relationship without saying whose it is.

Extraction and state recomputation are enqueued, never awaited in a request:
parsing a screenshot, extracting signals and recomputing state is tens of
seconds of model work.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import structlog
from fastapi import Depends, FastAPI, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import update

from ..agent import orchestrator
from ..config import settings
from ..domain.schemas.interaction import Channel
from ..storage.db import dyad_session, erasure_session
from ..storage.deletion import forget_person, inspect_person, orphaned_profiles
from ..storage.models import RelationshipRow, StrategyRecordRow
from ..storage.scope import Scope, UserScope
from .auth import (
    Identity,
    current_identity,
    owned_person,
    owned_relationship,
)

log = structlog.get_logger(__name__)
app = FastAPI(title="Sonder", version="0.1.0")

ACTIONS = {"sent_verbatim", "sent_edited", "discarded"}


def _user_scope(identity: Identity) -> UserScope:
    return UserScope(user_id=identity.user_id, identity_claim=identity.claim)


class ReplyIn(BaseModel):
    relationship_id: UUID
    text: str = Field(default="", max_length=2000)


class SuggestionOut(BaseModel):
    text: str
    tactic: str
    tone: str = Field(description="lighter | warmer | more ambiguous | more direct")
    score: float
    strategy_id: UUID | None


class ReplyOut(BaseModel):
    suggestions: list[SuggestionOut]
    objective: str
    rationale: str
    next_milestone: str
    health_warning: str | None
    notes: list[str]
    rejected_count: int


@app.get("/health")
async def health() -> dict:
    return {"ok": True, "version": app.version}


@app.post("/reply", response_model=ReplyOut)
async def reply(
    body: ReplyIn, identity: Identity = Depends(current_identity)
) -> ReplyOut:
    user_id = identity.user_id
    await owned_relationship(body.relationship_id, identity)
    scope = Scope(
        user_id=user_id,
        relationship_id=body.relationship_id,
        identity_claim=identity.claim,
    )
    request = orchestrator.route_request(body.text)
    async with dyad_session(scope) as session:
        result = await orchestrator.reply(session, scope, user_id, track=request.track)
    return ReplyOut(
        suggestions=[
            SuggestionOut(
                text=s.text,
                tactic=s.tactic,
                tone=s.register,
                score=s.score,
                strategy_id=s.strategy_id,
            )
            for s in result.suggestions
        ],
        objective=result.plan.objective.value,
        rationale=result.plan.rationale,
        next_milestone=result.plan.next_milestone,
        health_warning=result.health_warning,
        notes=result.notes,
        rejected_count=result.rejected_count,
    )


class AskIn(BaseModel):
    relationship_id: UUID
    question: str = Field(max_length=2000)


@app.post("/ask")
async def ask(body: AskIn, identity: Identity = Depends(current_identity)) -> dict:
    user_id = identity.user_id
    await owned_relationship(body.relationship_id, identity)
    scope = Scope(
        user_id=user_id,
        relationship_id=body.relationship_id,
        identity_claim=identity.claim,
    )
    request = orchestrator.route_request(body.question)
    intent = request.intent
    if intent.is_hot:
        intent = orchestrator.Intent.ANALYZE_CONVERSATION
    async with dyad_session(scope) as session:
        answer = await orchestrator.analyze(
            session,
            scope,
            request.text,
            intent,
            track=request.track,
        )
    return {"intent": intent.value, "answer": answer}


@app.post("/ingest")
async def ingest(
    relationship_id: UUID,
    channel: Channel,
    files: list[UploadFile],
    identity: Identity = Depends(current_identity),
) -> dict:
    """Accept screenshots, enqueue, return immediately."""
    from arq.connections import RedisSettings, create_pool

    user_id = identity.user_id
    await owned_relationship(relationship_id, identity)
    if len(files) > 20:
        raise HTTPException(400, "at most 20 screenshots per upload")
    payloads = [await f.read() for f in files]
    if sum(len(p) for p in payloads) > 40 * 1024 * 1024:
        raise HTTPException(400, "upload too large")

    redis = await create_pool(RedisSettings.from_dsn(settings().redis_url))
    job = await redis.enqueue_job(
        "ingest_screenshots",
        str(user_id),
        str(relationship_id),
        channel.value,
        payloads,
    )
    return {"job_id": job.job_id if job else None, "queued": len(payloads)}


class ActionIn(BaseModel):
    relationship_id: UUID
    strategy_id: UUID
    action: str = Field(description="sent_verbatim | sent_edited | discarded")


@app.post("/strategy/action")
async def strategy_action(
    body: ActionIn, identity: Identity = Depends(current_identity)
) -> dict:
    """Record what the user actually did with a suggestion.

    Without this the outcome loop has nothing to attribute: a suggestion that
    was rewritten or never sent must not train the model as though it were.
    """
    if body.action not in ACTIONS:
        raise HTTPException(400, "unknown action")
    user_id = identity.user_id
    await owned_relationship(body.relationship_id, identity)
    scope = Scope(
        user_id=user_id,
        relationship_id=body.relationship_id,
        identity_claim=identity.claim,
    )
    async with dyad_session(scope) as session:
        await session.execute(
            update(StrategyRecordRow)
            .where(
                StrategyRecordRow.id == body.strategy_id,
                StrategyRecordRow.relationship_id == scope.relationship_id,
            )
            .values(
                user_action=body.action,
                sent_at=datetime.now(UTC) if body.action.startswith("sent") else None,
            )
        )
    return {"ok": True}


# ------------------------------------------------------------------ erasure


@app.get("/person/{person_id}")
async def inspect(
    person_id: UUID = Depends(owned_person),
    identity: Identity = Depends(current_identity),
) -> dict:
    """Everything held about one person.

    Erasure is the control that makes modelling someone acceptable here, and
    a control you cannot inspect is not one you can trust. Read this before
    deciding to use it.
    """
    async with erasure_session(_user_scope(identity)) as session:
        result = await inspect_person(session, person_id)
    if not result.get("found"):
        raise HTTPException(404, "no such person")
    return result


@app.delete("/person/{person_id}")
async def forget_person_route(
    person_id: UUID = Depends(owned_person),
    identity: Identity = Depends(current_identity),
) -> dict:
    """Erase a person and everything ever inferred about them.

    Deleting only the relationship leaves the inferred profile behind, which
    is exactly the wrong half to keep.
    """
    async with erasure_session(_user_scope(identity)) as session:
        receipt = await forget_person(session, person_id)
    if receipt.display_name is None:
        raise HTTPException(404, "no such person")
    if not receipt.complete:
        # Reporting success on a partial erasure is worse than failing: the
        # user stops thinking about data that is still there.
        raise HTTPException(500, f"erasure incomplete: {receipt.residue}")
    return {
        "ok": True,
        "summary": receipt.summary(),
        "rows_removed": receipt.rows,
        "relationships_removed": receipt.relationships,
    }


@app.get("/residue")
async def residue(identity: Identity = Depends(current_identity)) -> dict:
    """Third-party profiles with no live relationship, or past retention."""
    async with erasure_session(_user_scope(identity)) as session:
        rows = await orphaned_profiles(session, identity.user_id)
    return {"count": len(rows), "profiles": rows}


@app.delete("/relationship/{relationship_id}")
async def forget_relationship(
    relationship_id: UUID = Depends(owned_relationship),
    identity: Identity = Depends(current_identity),
) -> dict:
    """Erase one relationship, keeping the person record.

    Narrower than DELETE /person on purpose: for when a relationship is over
    but you have not decided to erase the person. The profile survives, so
    /residue will list it afterwards.
    """
    from sqlalchemy import delete

    user_id = identity.user_id
    scope = Scope(
        user_id=user_id,
        relationship_id=relationship_id,
        identity_claim=identity.claim,
    )
    async with dyad_session(scope) as session:
        await session.execute(
            delete(RelationshipRow).where(
                RelationshipRow.id == relationship_id,
                RelationshipRow.user_id == user_id,
            )
        )
    log.info("relationship_erased", relationship=str(relationship_id)[:8])
    return {
        "ok": True,
        "cascade": "interactions, messages, signals, states, preferences, "
        "strategies, outcomes, memory, consent",
        "kept": "the person profile -- use DELETE /person to erase that too",
    }
