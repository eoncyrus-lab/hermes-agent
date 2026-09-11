"""Browser UI: one page, plus the two endpoints it needs to be usable.

The page is deliberately not a second product. It is the bot's text flow with
a textarea instead of a chat window, and it runs through the same
`orchestrator.handle`, the same `owned_relationship` check and the same
row-level policies -- so it cannot reach anything the Telegram bot could not.

Two endpoints exist here rather than in `main.py` because neither is part of
the product's API surface:

  GET  /ui             the page itself. No auth: it is static HTML, and every
                       call it makes carries a bearer token of its own.
  GET  /relationships  the picker's data. Same query as the bot's `/list`.
  POST /chat           `orchestrator.handle` -- the bot's one-text-box flow.

`/chat` is the gap this fills: `handle` classifies a request and takes the hot
path (reply candidates) or the slow path (analysis). The HTTP API had split
that into `/reply` and `/ask`, so a browser had to guess which one it wanted
before the classifier ever ran. The bot never had that problem.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from .. import channels
from ..agent import orchestrator
from ..config import settings
from ..llm.provider import ProviderError
from ..storage.db import dyad_session, user_session
from ..storage.models import PersonProfileRow, RelationshipRow
from ..storage.scope import Scope, UserScope
from .auth import Identity, current_identity, owned_relationship
from .queue import enqueue_text_ingest

log = structlog.get_logger(__name__)

router = APIRouter()

# Resolved from the source tree, the same way `config.skills_dir` is. The
# Dockerfile copies `sonder/`, so `sonder/api/static/` travels with it.
_PAGE = Path(__file__).resolve().parent / "static" / "ui.html"

# A placeholder the page carries so the token can be placed by replacement
# rather than by templating the whole document.
_AUTOLOGIN_SLOT = "<!--autologin-->"

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "[::1]"})
_LOOPBACK_CLIENTS = frozenset({"127.0.0.1", "::1"})


def _host_without_port(value: str) -> str:
    """`127.0.0.1:8000` and `[::1]:8000` both name a loopback host."""
    value = value.strip().lower()
    if value.startswith("["):
        return value.split("]", 1)[0] + "]"
    return value.split(":", 1)[0]


def _may_auto_login(request: Request) -> bool:
    """Whether this request is allowed to be handed the provisioned token.

    Two independent checks, and both are load-bearing:

    * the **Host header** must name a loopback host. This is what blunts DNS
      rebinding -- a page on `evil.example` can point that name at 127.0.0.1
      and make its fetches same-origin, at which point the browser would let it
      read this response. Such a request still arrives with
      `Host: evil.example`, so refusing any non-loopback name closes that path.
      Without this check the client-address test below would not help, because
      the request really does come from 127.0.0.1.
    * the **client address** must be loopback, so a request proxied in from
      elsewhere is refused even when it forges a loopback Host.

    What this does not defend against, stated plainly: any local process that
    can reach 127.0.0.1:8000 gets a working token for the configured account.
    On a single-user machine that process can already read `.env` and the
    database, so the token is not the weakest link -- but this is a decision
    about a personal machine, not a property that would hold on a shared host.
    """
    token = settings().ui_token
    if not token:
        return False
    if _host_without_port(request.headers.get("host", "")) not in _LOOPBACK_HOSTS:
        return False
    client = request.client.host if request.client else None
    return client in _LOOPBACK_CLIENTS


@router.get("/ui", response_class=HTMLResponse, include_in_schema=False)
async def ui(request: Request) -> HTMLResponse:
    page = _PAGE.read_text(encoding="utf-8")
    if _may_auto_login(request):
        # json.dumps rather than an f-string: this is the one place an
        # operator-supplied value is written into a document, and the quoting
        # has to survive a token containing a quote or a backslash.
        page = page.replace(
            _AUTOLOGIN_SLOT,
            f"<script>window.__SONDER_TOKEN__={json.dumps(settings().ui_token)}</script>",
        )
    # The response can carry a live credential, so it must not be reused from
    # a cache after that credential is rotated.
    return HTMLResponse(page, headers={"Cache-Control": "no-store"})


class RelationshipOut(BaseModel):
    id: UUID
    display_name: str | None


@router.get("/relationships", response_model=list[RelationshipOut])
async def relationships(
    identity: Identity = Depends(current_identity),
) -> list[RelationshipOut]:
    """The caller's relationships, for the picker.

    The same query the bot's `/list` runs, under the user-scoped policy: a
    session can only ever see rows belonging to the verified user, so there is
    no `WHERE user_id` here that a bug could drop.
    """
    scope = UserScope(user_id=identity.user_id, identity_claim=identity.claim)
    async with user_session(scope) as session:
        rows = (
            await session.execute(
                select(RelationshipRow.id, PersonProfileRow.display_name)
                .join(
                    PersonProfileRow,
                    PersonProfileRow.id == RelationshipRow.target_person_id,
                )
                .where(
                    RelationshipRow.user_id == identity.user_id,
                    PersonProfileRow.subject == "target",
                )
                .order_by(PersonProfileRow.display_name, RelationshipRow.id)
            )
        ).all()
    return [RelationshipOut(id=rid, display_name=name) for rid, name in rows]


class RelationshipIn(BaseModel):
    display_name: str = Field(min_length=1, max_length=80)
    retention_days: int = Field(default=365, ge=1, le=3650)


@router.post("/relationships", response_model=RelationshipOut, status_code=201)
async def create_relationship(
    body: RelationshipIn, identity: Identity = Depends(current_identity)
) -> RelationshipOut:
    """Create a third-party profile and the relationship pointing at it.

    The CLI has been able to do this since the first bootstrap commit. The page
    needs it because "no relationships exist" is a first-run state, and telling
    someone to leave the browser and run a command with a `<your user_id>`
    placeholder in it is not a first run -- the placeholder is not something
    they have any reason to know.

    `retention_until` is required by the schema for a third-party subject:
    modelling someone who is not your user is only acceptable with an expiry on
    it, so there is deliberately no path here that leaves it unset either.

    This runs under the user-scoped policy, not the maintenance role the CLI
    uses. `person_profiles_user_isolation` carries
    `user_id = app_current_user_id()` as its WITH CHECK, so the database itself
    constrains the row to the verified caller. The CLI holds a BYPASSRLS role
    and sets `user_id` in Python; this endpoint has no such latitude and does
    not need it.
    """
    scope = UserScope(user_id=identity.user_id, identity_claim=identity.claim)
    async with user_session(scope) as session:
        person = PersonProfileRow(
            user_id=identity.user_id,
            subject="target",
            data_subject="third_party",
            display_name=body.display_name,
            version=1,
            retention_until=datetime.now(UTC) + timedelta(days=body.retention_days),
        )
        session.add(person)
        await session.flush()
        relationship = RelationshipRow(
            user_id=identity.user_id, target_person_id=person.id
        )
        session.add(relationship)
        await session.flush()
        relationship_id = relationship.id
    return RelationshipOut(id=relationship_id, display_name=body.display_name)


class ChatIn(BaseModel):
    relationship_id: UUID
    # Matches ReplyIn: the box carries the conversation, not just an
    # instruction, so the cap has to fit a pasted exchange.
    text: str = Field(max_length=8000)


@router.post("/chat")
async def chat(body: ChatIn, identity: Identity = Depends(current_identity)) -> dict:
    """The bot's `on_text`, over HTTP.

    Returns a tagged union rather than a fixed shape, because `handle` decides
    at runtime which of the two it is: `{"kind": "answer"}` for the slow path,
    `{"kind": "suggestions"}` for the hot one. Collapsing that into one shape
    would mean inventing a "suggestion" for a question that has no reply in it.
    """
    user_id = identity.user_id
    await owned_relationship(body.relationship_id, identity)
    scope = Scope(
        user_id=user_id,
        relationship_id=body.relationship_id,
        identity_claim=identity.claim,
    )
    # Asked for before the models run, because the answer is already on disk
    # and does not need an agent loop. Handled here rather than in each front
    # end so all three surfaces answer the same words.
    if body.text.strip() in channels.BRIEF_WORDS:
        return {"kind": "briefing", "text": await channels.briefing(scope)}

    try:
        async with dyad_session(scope) as session:
            result = await orchestrator.handle(session, scope, user_id, body.text)
    except ProviderError as exc:
        # A 500 tells the user nothing they can act on. The reason is logged;
        # the page gets a sentence.
        log.warning("chat_provider_failed", error=str(exc))
        return {"kind": "answer", "text": channels.PROVIDER_FAILED, "failed": True}

    # The same second half as `/reply`: the answer above was drafted from what
    # the user sent and did not wait on a queue, and this is what turns that
    # text into an observation the state model can learn from. Reported rather
    # than raised, because the answer already exists.
    persisted = await enqueue_text_ingest(user_id, body.relationship_id, body.text)

    if isinstance(result, str):
        return {"kind": "answer", "text": result, "persisted": persisted}

    return {
        "kind": "suggestions",
        "persisted": persisted,
        "suggestions": [
            {
                "text": s.text,
                "tactic": s.tactic,
                "tone": s.register,
                "score": s.score,
                "strategy_id": str(s.strategy_id) if s.strategy_id else None,
            }
            for s in result.suggestions
        ],
        # Rendered here, from the same function the two chat channels use. The
        # page used to translate the enum values itself with its own label map,
        # which is how three surfaces start saying three slightly different
        # things about the same suggestion.
        "detail": channels.render_detail(result),
        "objective": result.plan.objective.value,
        "rationale": result.plan.rationale,
        "next_milestone": result.plan.next_milestone,
        "health_warning": result.health_warning,
        "notes": result.notes,
        "rejected_count": result.rejected_count,
    }
