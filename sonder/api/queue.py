"""Enqueueing background work, kept in one place.

Every request path that hands work to the worker addresses the queue the same
way, and the ingest jobs must not drift in how they are named or in the order
of their arguments: the API and the worker are separate processes with no
shared type checking between them, so a mismatch is a runtime surprise rather
than an import error.
"""

from __future__ import annotations

import structlog
from arq.connections import RedisSettings, create_pool

from ..config import settings
from ..domain.schemas.interaction import Channel

log = structlog.get_logger(__name__)


async def enqueue(job: str, *args) -> str | None:
    """Queue one job and return its id, or None if that job already exists.

    A pool per call is what the ingest route has always done: `create_pool` is
    cheap, and a request that enqueues one job does not justify holding a
    connection open on the API. What it did not do was close it, so every
    upload leaked one. `aclose` sits in a `finally` because a failed enqueue
    leaks exactly as much as a successful one.
    """
    redis = await create_pool(RedisSettings.from_dsn(settings().redis_url))
    try:
        queued = await redis.enqueue_job(job, *args)
    finally:
        await redis.aclose()
    return queued.job_id if queued else None


async def enqueue_text_ingest(user_id, relationship_id, text: str) -> bool:
    """Hand a conversation the user just supplied to the ingest pipeline.

    This is the second half of the hot path. The reply is drafted from the text
    the caller sent, immediately, without waiting on a queue; this call is how
    the same text stops being request-scoped and becomes an observation with an
    evidence ref that the state model can learn from.

    `Channel.OTHER` because the channel is genuinely unknown -- the queue
    records *how* the material arrived (`IngestSource.MANUAL_ENTRY` on the
    worker side), not where it happened, and naming an app nobody named would
    be a claim the user never made.

    Returns whether it was queued. False is not something the caller should
    raise on: by the time this runs the user's reply already exists, and
    failing the request would take that away over a background job. It is
    reported anyway, because a conversation that never reaches the pipeline is
    one the next reply will still be drafted against nothing.
    """
    body = text.strip()
    if not body:
        return False
    try:
        await enqueue(
            "ingest_text",
            str(user_id),
            str(relationship_id),
            Channel.OTHER.value,
            body,
        )
    except Exception as exc:
        log.warning("ingest_text_enqueue_failed", error=str(exc))
        return False
    return True
