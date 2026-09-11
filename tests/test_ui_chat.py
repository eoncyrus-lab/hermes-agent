"""The browser page's two endpoints, and what they promise.

`/chat` returns a tagged union because `orchestrator.handle` decides at
runtime which of three things a message is: a considered answer, a set of
reply candidates, or a request for what sonder knows. The page switches on
`kind` and renders what it is given.

The property worth pinning is that it renders *what it is given*. The page
used to hold its own copies of the objective and tone label maps and translate
the enum values itself, which is how three surfaces start saying three
slightly different things about the same suggestion. The wording now comes
from `sonder/channels.py`, where the two chat channels get it too.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest

from sonder import channels
from sonder.api import ui
from sonder.api.auth import Identity


@asynccontextmanager
async def _session(_scope):
    yield object()


async def _owned(_relationship_id, _identity):
    return None


def _identity() -> Identity:
    return Identity(user_id=uuid4(), claim="test-claim")


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setattr(ui, "owned_relationship", _owned)
    monkeypatch.setattr(ui, "dyad_session", _session)

    async def _no_persist(*_a, **_kw):
        return True

    monkeypatch.setattr(ui, "enqueue_text_ingest", _no_persist)


def _suggestion_result():
    return SimpleNamespace(
        suggestions=[
            SimpleNamespace(
                text="哪座山",
                tactic="curiosity",
                register="lighter",
                score=0.9,
                strategy_id=uuid4(),
            ),
            SimpleNamespace(
                text="几点出发",
                tactic="direct",
                register="more direct",
                score=0.6,
                strategy_id=None,
            ),
        ],
        plan=SimpleNamespace(
            objective=SimpleNamespace(value="increase_engagement"),
            rationale="跟她之间的底子还很薄",
            next_milestone="不用你追问，她会自己接着聊下去",
        ),
        health_warning=None,
        notes=[],
        rejected_count=2,
    )


# ------------------------------------------------------------------ briefing


async def test_a_briefing_request_does_not_run_the_models(monkeypatch, wired):
    """The answer is already on disk. Anything that calls a model here is
    spending seconds and money to say what a query already knows."""
    called = []

    async def _handle(*_a, **_kw):  # pragma: no cover - must not be reached
        called.append(True)
        raise AssertionError("the briefing ran the agent")

    async def _briefing(_scope):
        return "我手上关于她的材料：13 条消息"

    monkeypatch.setattr(ui.orchestrator, "handle", _handle)
    monkeypatch.setattr(channels, "briefing", _briefing)

    result = await ui.chat(
        ui.ChatIn(relationship_id=uuid4(), text="你知道她什么"), identity=_identity()
    )

    assert called == []
    assert result["kind"] == "briefing"
    assert "13 条消息" in result["text"]


@pytest.mark.parametrize("phrase", ["你知道什么", "你都知道她什么", "她的情况", "简报"])
async def test_every_shared_trigger_word_reaches_the_briefing(monkeypatch, wired, phrase):
    """Recognised server-side rather than in each front end, so all three
    surfaces answer the same words."""

    async def _briefing(_scope):
        return "brief"

    called = []

    async def _handle(*_a, **_kw):  # pragma: no cover
        called.append(True)

    monkeypatch.setattr(channels, "briefing", _briefing)
    monkeypatch.setattr(ui.orchestrator, "handle", _handle)

    result = await ui.chat(
        ui.ChatIn(relationship_id=uuid4(), text=phrase), identity=_identity()
    )

    assert result["kind"] == "briefing" and not called


async def test_a_sentence_containing_the_words_is_still_a_message(monkeypatch, wired):
    """Whole-message matches only. "她说你知道什么" is a question about her."""
    asked = []

    async def _briefing(_scope):  # pragma: no cover - must not be reached
        asked.append(True)
        return "brief"

    async def _handle(*_a, **_kw):
        return "分析结果"

    monkeypatch.setattr(channels, "briefing", _briefing)
    monkeypatch.setattr(ui.orchestrator, "handle", _handle)

    result = await ui.chat(
        ui.ChatIn(relationship_id=uuid4(), text="她说你知道什么"),
        identity=_identity(),
    )

    assert asked == [] and result["kind"] == "answer"


# ------------------------------------------------------------------ detail


async def test_the_page_is_handed_wording_not_enums(monkeypatch, wired):
    async def _handle(*_a, **_kw):
        return _suggestion_result()

    monkeypatch.setattr(ui.orchestrator, "handle", _handle)

    result = await ui.chat(
        ui.ChatIn(relationship_id=uuid4(), text="她刚说周末想去爬山"),
        identity=_identity(),
    )

    assert result["kind"] == "suggestions"
    detail = result["detail"]
    assert "把互动拉起来" in detail, "the enum is not what a person reads"
    assert "increase_engagement" not in detail
    assert "更直接" in detail, "the tone label is translated here, not in JS"


async def test_the_detail_is_the_same_function_the_chat_channels_use(monkeypatch, wired):
    """One implementation, three surfaces -- asserted rather than assumed,
    because the whole point is that they cannot drift."""
    result_object = _suggestion_result()

    async def _handle(*_a, **_kw):
        return result_object

    monkeypatch.setattr(ui.orchestrator, "handle", _handle)

    result = await ui.chat(
        ui.ChatIn(relationship_id=uuid4(), text="hi"), identity=_identity()
    )

    assert result["detail"] == channels.render_detail(result_object)


# -------------------------------------------------------- when the model fails


async def test_a_provider_failure_is_a_sentence_not_a_500(monkeypatch, wired):
    """The user got a 500 they could do nothing with, and WeChat got nothing
    at all -- the exception was logged and no reply was sent, so the bot
    looked like it had gone quiet. Both now say the same thing."""
    from sonder.llm.provider import ProviderError

    async def _handle(*_a, **_kw):
        raise ProviderError("provider 'deepseek' returned no text")

    monkeypatch.setattr(ui.orchestrator, "handle", _handle)

    result = await ui.chat(
        ui.ChatIn(relationship_id=uuid4(), text="她这句话什么意思"),
        identity=_identity(),
    )

    assert result["kind"] == "answer"
    assert result.get("failed") is True
    assert "模型" in result["text"]
    assert result["text"] == channels.PROVIDER_FAILED


async def test_the_failure_sentence_is_shared_with_the_chat_channels():
    """One wording, so the three surfaces cannot disagree about what went
    wrong -- the same reason the reply and the briefing live in one place."""
    from sonder.wechat import runner

    assert runner.channels.PROVIDER_FAILED is channels.PROVIDER_FAILED
