"""Background jobs. Everything slow, expensive, or scheduled.

The ordering here is the pipeline: parse -> features -> signals -> state ->
preferences. Each step writes its own rows so a failure part-way leaves
auditable partial progress rather than nothing.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, UUID, uuid5

import structlog
from arq import cron
from arq.connections import RedisSettings

from ..analyzer import ingest as ingest_mod
from ..analyzer.features import Baseline
from ..analyzer.signals import extract_signals
from ..config import settings
from ..domain.schemas.interaction import Channel, Speaker
from ..learning import outcomes
from ..state import preference as pref_state
from ..state import relationship as state_engine
from ..storage.db import maintenance_session, worker_session
from ..storage.repositories import (
    InteractionRepo,
    PreferenceRepo,
    PriorRepo,
    SignalRepo,
    StateRepo,
    StrategyRepo,
)
from ..storage.scope import Scope, UserScope

log = structlog.get_logger(__name__)


class OwnershipMismatch(RuntimeError):
    """A job named a relationship its user does not own.

    A queued job carries no bearer token, so `sonder_worker`'s policies can
    only enforce the pinned relationship -- they cannot tell whose it is. That
    half is enforced here. It should be unreachable: the API checks ownership
    before enqueueing. It is checked anyway because the queue is a second way
    into this code, and "the caller already validated it" is exactly the
    assumption that makes a cross-account read possible later.
    """


async def authorize(session, scope: Scope):
    """Return the pinned relationship, or None if it no longer exists.

    Raises rather than returning None when the relationship exists but belongs
    to someone else: a missing row is ordinary (erased between enqueue and
    run), a mismatched owner is not.
    """
    from ..storage.models import RelationshipRow

    rel = await session.get(RelationshipRow, scope.relationship_id)
    if rel is None:
        return None
    if rel.user_id != scope.user_id:
        raise OwnershipMismatch(
            f"job for relationship {str(scope.relationship_id)[:8]} claims a "
            "user that does not own it"
        )
    return rel


def _screenshot_ingest_id(
    relationship_id: UUID, channel: str, images: list[bytes]
) -> UUID:
    digest = hashlib.sha256()
    for image in images:
        digest.update(len(image).to_bytes(8, "big"))
        digest.update(image)
    return uuid5(
        NAMESPACE_URL,
        f"sonder:screenshot:{relationship_id}:{channel}:{digest.hexdigest()}",
    )


def _with_stable_ingest_ids(interaction, interaction_id: UUID):
    messages = tuple(
        message.model_copy(
            update={
                "id": uuid5(
                    interaction_id,
                    f"message:{message.seq}:{message.sender.value}:{message.text_sha256}",
                ),
                "interaction_id": interaction_id,
            }
        )
        for message in interaction.messages
    )
    return interaction.model_copy(update={"id": interaction_id, "messages": messages})


async def ingest_screenshots(
    ctx, user_id: str, relationship_id: str, channel: str, images: list[bytes]
) -> dict:
    scope = Scope(UUID(user_id), UUID(relationship_id))
    ingest_id = _screenshot_ingest_id(scope.relationship_id, channel, images)

    # ARQ retries and duplicate deliveries must converge on one interaction.
    # This early read avoids another vision/signal extraction call on replay.
    # State recomputation still runs below: the previous attempt may have
    # committed the interaction and signals, then crashed before that stage.
    async with worker_session(scope) as session:
        from ..storage.models import InteractionRow

        if await authorize(session, scope) is None:
            return {"skipped": "no such relationship"}
        duplicate = await session.get(InteractionRow, ingest_id) is not None
    if duplicate:
        await recompute_state(ctx, user_id, relationship_id)
        return {"interaction_id": str(ingest_id), "duplicate": True}

    parsed = await ingest_mod.parse_screenshots(images)
    interaction = ingest_mod.to_interaction(
        parsed, scope.relationship_id, Channel(channel)
    )
    interaction = _with_stable_ingest_ids(interaction, ingest_id)

    async with worker_session(scope) as session:
        repo = InteractionRepo(session)
        await repo.add(scope, interaction)

        history = await repo.last_messages(scope, 200)
        baseline = Baseline.from_rows(history, Speaker.TARGET)
        signals = await extract_signals(
            list(interaction.messages),
            scope.relationship_id,
            interaction.id,
            baseline,
        )
        await SignalRepo(session).add_many(scope, signals)

    await recompute_state(ctx, user_id, relationship_id)
    log.info(
        "ingest_complete",
        messages=len(interaction.messages),
        signals=len(signals),
        unreadable=parsed.unreadable_regions,
    )
    return {
        "interaction_id": str(interaction.id),
        "messages": len(interaction.messages),
        "signals": len(signals),
        "unreadable_regions": parsed.unreadable_regions,
    }


async def recompute_state(ctx, user_id: str, relationship_id: str) -> dict:
    scope = Scope(UUID(user_id), UUID(relationship_id))
    async with worker_session(scope) as session:
        rel = await authorize(session, scope)
        if rel is None:
            return {"skipped": "no such relationship"}

        signal_rows = await SignalRepo(session).since(scope, days=120)
        from ..domain.schemas.interaction import Signal, SignalType

        signals = [
            Signal(
                id=r.id,
                relationship_id=r.relationship_id,
                interaction_id=r.interaction_id,
                type=SignalType(r.type),
                occurred_at=r.occurred_at,
                magnitude=r.magnitude,
                confidence=r.confidence,
                by=Speaker(r.by_speaker),
                evidence_message_ids=tuple(r.evidence_message_ids),
                baseline_note=r.baseline_note,
                extractor_version=r.extractor_version,
            )
            for r in signal_rows
        ]

        states = StateRepo(session)
        current = await states.current(scope)
        interaction_ids = [
            i.id for i in await InteractionRepo(session).recent(scope, limit=50)
        ]
        new_state = state_engine.compute(
            signals,
            relationship_id=scope.relationship_id,
            user_id=scope.user_id,
            target_person_id=rel.target_person_id,
            version=(current.version + 1) if current else 1,
            supersedes=current.id if current else None,
            interaction_ids=interaction_ids,
        )
        # Keyed on the inputs, so an ARQ retry or a duplicate delivery
        # converges on the snapshot it already produced instead of appending
        # an identical one and bumping the version.
        written_id = await states.append(
            scope,
            new_state,
            input_fingerprint=state_engine.input_fingerprint(signals, interaction_ids),
        )
        # `append` converges rather than inserting when a replay arrives, so
        # the snapshot on disk may be the previous attempt's, not the object
        # just computed. Report the row that exists.
        persisted = await states.current(scope)
        converged = written_id != new_state.id

        prefs = PreferenceRepo(session)
        rows = await prefs.evidence(scope)
        if rows:
            async with maintenance_session() as admin:
                priors = await PriorRepo(admin).for_user(UserScope(scope.user_id))
            existing = await prefs.current_model(scope)
            model = pref_state.rebuild(
                rel.target_person_id,
                scope.relationship_id,
                rows,
                priors,
                version=(existing.version + 1) if existing else 1,
            )
            await prefs.save_model(
                scope,
                model,
                input_fingerprint=pref_state.input_fingerprint(rows, priors),
            )

    version = persisted.version if persisted is not None else new_state.version
    stage = persisted.stage if persisted is not None else new_state.stage.value
    return {"state_version": version, "stage": stage, "converged": converged}


async def score_outcomes(ctx) -> dict:
    """Close observation windows. Scheduled, never triggered by a request."""
    from sqlalchemy import select

    from ..storage.models import RelationshipRow

    scored = 0
    async with maintenance_session() as admin:
        rels = list((await admin.execute(select(RelationshipRow))).scalars())

    for rel in rels:
        scope = Scope(rel.user_id, rel.id)
        async with worker_session(scope) as session:
            strategies = StrategyRepo(session)
            due = await strategies.awaiting_outcome(scope)
            if not due:
                continue
            interactions = InteractionRepo(session)
            signals_repo = SignalRepo(session)
            for row in due:
                start = row.sent_at
                end = start + timedelta(seconds=row.observation_window_s)
                all_msgs = await interactions.last_messages(scope, 400)
                window = outcomes.Window(
                    start=start,
                    end=end,
                    signals=[
                        s
                        for s in await signals_repo.since(scope, days=30)
                        if start <= s.occurred_at <= end
                    ],
                    messages_before=[m for m in all_msgs if m.sent_at < start],
                    messages_after=[m for m in all_msgs if start <= m.sent_at <= end],
                )
                outcome = outcomes.score(row, window)
                await strategies.add_outcome(scope, outcome)

                evidence = outcomes.to_preference_evidence(
                    row, outcome, rel.target_person_id, None
                )
                if evidence:
                    await PreferenceRepo(session).add_evidence(scope, evidence)
                scored += 1

    log.info("outcomes_scored", n=scored)
    return {"scored": scored}


async def purge_artifacts(ctx) -> dict:
    """Delete source screenshots past their TTL (doc s51)."""
    from sqlalchemy import update

    from ..storage.models import InteractionRow

    cutoff = datetime.now(UTC) - timedelta(hours=settings().artifact_ttl_hours)
    async with maintenance_session() as admin:
        result = await admin.execute(
            update(InteractionRow)
            .where(
                InteractionRow.source_artifact_id.isnot(None),
                InteractionRow.source_artifact_deleted.is_(False),
                InteractionRow.ingested_at < cutoff,
            )
            .values(source_artifact_id=None, source_artifact_deleted=True)
        )
    log.info("artifacts_purged", n=result.rowcount)
    return {"purged": result.rowcount}


class WorkerSettings:
    functions = [ingest_screenshots, recompute_state, score_outcomes, purge_artifacts]
    cron_jobs = [
        cron(score_outcomes, hour=None, minute={7, 37}),
        cron(purge_artifacts, hour={3}, minute={0}),
    ]

    @staticmethod
    def redis_settings() -> RedisSettings:
        return RedisSettings.from_dsn(settings().redis_url)
