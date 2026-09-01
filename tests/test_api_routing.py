from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest

from sonder.api import main
from sonder.api.auth import Identity
from sonder.llm import Task, Track


@asynccontextmanager
async def _session(_scope):
    yield object()


def _identity(user_id=None) -> Identity:
    return Identity(user_id=user_id or uuid4(), claim="test-claim")


async def _owned(_relationship_id, _identity):
    """Same shape as the real dependency, so a signature change fails here."""
    return None


@pytest.mark.asyncio
async def test_reply_endpoint_passes_explicit_track(monkeypatch):
    seen = {}

    async def fake_reply(_session, _scope, _user_id, *, track):
        seen["track"] = track
        return SimpleNamespace(
            suggestions=[],
            plan=SimpleNamespace(
                objective=SimpleNamespace(value="maintain"),
                rationale="test",
                next_milestone="test",
            ),
            health_warning=None,
            notes=[],
            rejected_count=0,
        )

    monkeypatch.setattr(main, "owned_relationship", _owned)
    monkeypatch.setattr(main, "dyad_session", _session)
    monkeypatch.setattr(main.orchestrator, "reply", fake_reply)

    await main.reply(
        main.ReplyIn(relationship_id=uuid4(), text="/intimate 想试试新东西"),
        identity=_identity(),
    )
    assert seen["track"] is Track.INTIMATE


@pytest.mark.asyncio
async def test_ask_endpoint_strips_prefix_and_passes_track(monkeypatch):
    seen = {}

    async def fake_analyze(_session, _scope, question, intent, *, track):
        seen.update(question=question, intent=intent, track=track)
        return "ok"

    monkeypatch.setattr(main, "owned_relationship", _owned)
    monkeypatch.setattr(main, "dyad_session", _session)
    monkeypatch.setattr(main.orchestrator, "analyze", fake_analyze)

    result = await main.ask(
        main.AskIn(relationship_id=uuid4(), question="/intimate 她想试试新东西"),
        identity=_identity(),
    )
    assert result["answer"] == "ok"
    assert seen["question"] == "她想试试新东西"
    assert seen["track"] is Track.INTIMATE


@pytest.mark.asyncio
async def test_intimate_request_keeps_safety_tasks_on_standard_track(monkeypatch):
    """Spy at the router boundary while exercising the real orchestration chain."""
    monkeypatch.setenv("SONDER_DATABASE_URL", "postgresql+asyncpg://app:test@db/sonder")
    from sonder.agent import critic, generator, humanizer, orchestrator
    from sonder.agent.context import AgentContext
    from sonder.analyzer import signals
    from sonder.llm.provider import Completion

    class SpyRouter:
        def __init__(self):
            self.calls = []

        async def complete(self, task, _messages, *, track=Track.STANDARD, **_kw):
            self.calls.append((task, track))
            text = "ok" if task is Task.REASON else "聊聊吗"
            return Completion(text=text, model="spy")

        async def structured(
            self, task, _messages, schema, *, track=Track.STANDARD, **_kw
        ):
            self.calls.append((task, track))
            if task is Task.GENERATE:
                return schema(
                    candidates=[
                        {"text": "可以聊聊吗", "tactic": "curiosity", "tone": "direct"}
                    ]
                )
            if task is Task.CRITIC:
                return schema(
                    naturalness=0.9,
                    voice_match=0.9,
                    context_match=0.9,
                    strategy_match=0.9,
                    tension=0.3,
                    cringe_risk=0.0,
                    pressure=0.0,
                    manipulation_risk=0.0,
                    boundary_risk=0.0,
                    note="safe",
                )
            if task is Task.EXTRACT:
                return schema(signals=[])
            raise AssertionError(f"unexpected task: {task}")

    spy = SpyRouter()
    for module in (orchestrator, generator, humanizer, critic, signals):
        monkeypatch.setattr(module, "router", lambda: spy)

    async def context(_session, scope):
        return AgentContext(scope=scope)

    monkeypatch.setattr(orchestrator.ctx_mod, "build", context)
    monkeypatch.setattr(main, "owned_relationship", _owned)
    monkeypatch.setattr(main, "dyad_session", _session)

    relationship_id = uuid4()
    identity = _identity()
    await main.ask(
        main.AskIn(
            relationship_id=relationship_id,
            question="/intimate 她想试试新东西",
        ),
        identity=identity,
    )
    result = await main.reply(
        main.ReplyIn(
            relationship_id=relationship_id,
            text="/intimate 想回复得直接一点",
        ),
        identity=identity,
    )
    assert result.suggestions

    message = SimpleNamespace(
        id=uuid4(), sender="target", text="好", reply_latency_s=None
    )
    await signals.extract_signals([message], relationship_id, uuid4())

    assert spy.calls == [
        (Task.REASON, Track.INTIMATE),
        (Task.GENERATE, Track.INTIMATE),
        (Task.HUMANIZE, Track.INTIMATE),
        (Task.CRITIC, Track.STANDARD),
        (Task.EXTRACT, Track.STANDARD),
    ]


# ---------------------------------------------------------------- the wiring
#
# The tests above call the endpoint functions directly, which is fast but
# cannot see how FastAPI assembles their dependencies. That gap hid a real
# break: `owned_relationship` changed shape, four routes kept passing the old
# one, and every relationship-scoped endpoint raised AttributeError before
# doing any work while the suite stayed green. These go through the app.


@pytest.fixture
def client(monkeypatch):
    """A TestClient with a signed-in caller and no database behind it.

    Only identity is overridden. `owned_relationship` runs its real body, so
    the routes have to hand it what it actually takes.
    """
    from fastapi.testclient import TestClient

    from sonder.api.auth import current_identity

    identity = _identity()

    class _Result:
        def scalar(self):
            return identity.user_id

    class _Session:
        async def execute(self, _query):
            return _Result()

    @asynccontextmanager
    async def _user_session(scope):
        assert scope.identity_claim == identity.claim, "route dropped the claim"
        yield _Session()

    monkeypatch.setattr("sonder.api.auth.user_session", _user_session)
    monkeypatch.setattr(main, "dyad_session", _session)
    main.app.dependency_overrides[current_identity] = lambda: identity
    try:
        yield TestClient(main.app), identity
    finally:
        main.app.dependency_overrides.clear()


def test_every_relationship_route_survives_its_own_dependencies(client, monkeypatch):
    from sonder.agent import orchestrator

    http, _identity_used = client

    async def fake_reply(_session, _scope, _user_id, *, track):
        return SimpleNamespace(
            suggestions=[],
            plan=SimpleNamespace(
                objective=SimpleNamespace(value="maintain"),
                rationale="r",
                next_milestone="m",
            ),
            health_warning=None,
            notes=[],
            rejected_count=0,
        )

    async def fake_analyze(_session, _scope, _q, _intent, *, track):
        return "ok"

    monkeypatch.setattr(orchestrator, "reply", fake_reply)
    monkeypatch.setattr(orchestrator, "analyze", fake_analyze)

    rid = str(uuid4())
    assert (
        http.post("/reply", json={"relationship_id": rid, "text": "hi"}).status_code
        == 200
    )
    assert (
        http.post("/ask", json={"relationship_id": rid, "question": "hi"}).status_code
        == 200
    )
    # Rejected on the action name, which is still past every dependency.
    assert (
        http.post(
            "/strategy/action",
            json={
                "relationship_id": rid,
                "strategy_id": str(uuid4()),
                "action": "nonsense",
            },
        ).status_code
        == 400
    )


def test_an_unauthenticated_request_is_refused(monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setattr("sonder.api.auth.user_session", None)
    http = TestClient(main.app)
    response = http.post("/reply", json={"relationship_id": str(uuid4()), "text": "hi"})
    assert response.status_code == 401
