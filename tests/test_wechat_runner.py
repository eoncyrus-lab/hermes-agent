"""The WeChat runner's decisions, without a WeChat account behind them.

The network half of this channel needs a paired bot and a human with a phone,
so what is pinned here is the part that is pure: how a message becomes a
reply, which commands exist, what happens to the cursor when the disk
disagrees, and what the channel does with a forwarded picture. The channel's
identity boundary is checked against real Postgres in
`tests/test_identity_boundary.py`; this file must not pretend to cover it.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from sonder import channels
from sonder.wechat import media, runner


class _FakeClient:
    """Records what the channel said, and nothing else."""

    def __init__(self) -> None:
        self.said: list[str] = []

    async def reply(self, _message, text: str) -> None:
        self.said.append(text)


def _image_message() -> SimpleNamespace:
    return SimpleNamespace(
        from_user_id="o9cq@im.wechat",
        raw={
            "item_list": [
                {
                    "type": 2,
                    "image_item": {"media": {"full_url": "https://cdn.example/x"}},
                }
            ]
        },
    )


def _result(**overrides):
    base = {
        "suggestions": [
            SimpleNamespace(
                text="在忙什么呢？",
                tactic="curiosity",
                register="lighter",
                score=0.8,
                strategy_id=None,
            )
        ],
        "plan": SimpleNamespace(
            objective=SimpleNamespace(value="establish_rapport"),
            rationale="no history yet",
            next_milestone="she asks something back",
        ),
        "health_warning": None,
        "notes": [],
        "rejected_count": 0,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_the_default_reply_is_the_sentence_and_nothing_else():
    """The user is standing in a chat window. Everything else -- objective,
    rationale, milestone, notes, alternatives -- goes behind a question.

    Measured against a real suggestion this was 307 characters of which 15
    were the sentence. The replacement is under 40, and the assertion is a
    budget rather than a shape so it cannot drift back one clause at a time.
    """
    rendered = channels.render_reply(_result(), runner.HINT)
    assert rendered.startswith("在忙什么呢？")
    assert len(rendered) < 80
    # The detail block is the dash-prefixed lines and the alternatives list.
    # The hint may say 理由 -- that tells the user asking is possible, which
    # is not the same thing as answering.
    assert "— " not in rendered
    assert "其他说法" not in rendered


def test_the_reply_says_how_to_record_what_happened():
    """Telegram has three inline buttons. There is nothing to press here, so
    the outcomes have to be stated -- and as one keystroke, because this is
    asked after every suggestion and the difference between "/sent" and "1"
    is the difference between the loop being used and being skipped."""
    rendered = channels.render_reply(_result(), runner.HINT)
    for digit in ("1", "2", "3"):
        assert digit in rendered


def test_alternatives_are_one_question_away():
    """They are still computed and still offered -- just not in the message
    that has to be scrolled past to reach the sentence."""
    result = _result(
        suggestions=[
            SimpleNamespace(
                text="首选", tactic="t", register="lighter", score=0.9, strategy_id=None
            ),
            SimpleNamespace(
                text="备选", tactic="t", register="warmer", score=0.5, strategy_id=None
            ),
        ]
    )
    assert "备选" not in channels.render_reply(result, runner.HINT)
    assert "备选" in channels.render_detail(result)


def test_the_detail_names_the_objective_in_words_a_person_uses():
    """`establish rapport` is an enum value, and the user reads Chinese."""
    detail = channels.render_detail(_result())
    assert "establish_rapport" not in detail
    assert "想达到" in detail


@pytest.mark.parametrize(
    ("command", "action"),
    [("sent", "sent_verbatim"), ("edited", "sent_edited"), ("skipped", "discarded")],
)
def test_every_outcome_the_api_accepts_has_a_command(command, action):
    """The API validates `action` against its own set, so a command naming
    something else would be rejected only after the user typed it."""
    assert runner.COMMANDS[command] == action
    assert action in runner.ACTIONS


def test_an_unlinked_chat_is_told_the_handle_to_send():
    """A bare "not linked" is a dead end -- the handle is not visible to the
    person in the chat, and it is what the operator needs to link them."""
    message = runner._unlinked_message("o9cq800kum@im.wechat")
    assert "o9cq800kum@im.wechat" in message


def test_the_cursor_round_trips(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(runner, "_CURSOR_FILE", tmp_path / "cursor")
    assert runner._load_cursor() == ""
    runner._save_cursor("abc123")
    assert runner._load_cursor() == "abc123"


def test_a_missing_cursor_directory_is_created(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(runner, "_CURSOR_FILE", tmp_path / "nested" / "cursor")
    runner._save_cursor("abc")
    assert runner._load_cursor() == "abc"


def test_an_unwritable_cursor_is_not_fatal(tmp_path: Path, monkeypatch):
    """Losing the cursor replays a backlog, which converges: ingest is
    content-addressed, so a redelivered conversation deduplicates. Refusing to
    run over a state file would be worse than the replay."""
    monkeypatch.setattr(runner, "_CURSOR_FILE", tmp_path / "cursor")
    tmp_path.chmod(0o500)
    try:
        runner._save_cursor("abc")  # must not raise
    finally:
        tmp_path.chmod(0o700)


# ------------------------------------------------------- a forwarded picture


async def test_a_picture_with_no_relationship_resolved_is_not_filed_anywhere(
    monkeypatch,
):
    """A picture with nowhere to go must not be guessed at: guessing would
    write a real person's words into the wrong person's record."""
    downloaded = []

    async def _never(_ref, **_kw):  # pragma: no cover - must not be reached
        downloaded.append(True)
        raise AssertionError("downloaded before a relationship was resolved")

    async def _none(_identity, _chat_id, _selection):
        return None

    async def _no_rows(_identity):
        return []

    monkeypatch.setattr(media, "download_image", _never)
    monkeypatch.setattr(channels, "current", _none)
    monkeypatch.setattr(channels, "relationships", _no_rows)

    client = _FakeClient()
    await runner._ingest_image(
        client,
        _image_message(),
        SimpleNamespace(user_id=uuid4(), claim="c"),
        media.ImageRef(url="https://cdn.example/x"),
    )

    assert downloaded == []
    assert "还没有任何对象" in client.said[0]


async def test_a_picture_that_cannot_be_fetched_says_so(monkeypatch):
    """A key that fails to decrypt and a CDN that refused are different
    problems, but both end with the user told rather than left waiting."""

    async def _boom(_ref, **_kw):
        raise media.MediaError("CDN download returned 404")

    async def _chosen(_identity, _chat_id, _selection):
        return (uuid4(), "她")

    monkeypatch.setattr(media, "download_image", _boom)
    monkeypatch.setattr(channels, "current", _chosen)

    client = _FakeClient()
    await runner._ingest_image(
        client,
        _image_message(),
        SimpleNamespace(user_id=uuid4(), claim="c"),
        media.ImageRef(url="https://cdn.example/x"),
    )

    assert "拿不到" in client.said[0]


async def test_a_picture_is_queued_for_the_same_ingest_the_api_uses(monkeypatch):
    """A picture over WeChat and a picture over the API are the same
    observation. The vision call, the evidence refs and the artifact TTL must
    not depend on which door it came through."""
    queued = []
    relationship_id, user_id = uuid4(), uuid4()

    async def _fake_download(_ref, **_kw):
        return b"jpeg bytes"

    async def _fake_enqueue(job, *args):
        queued.append((job, args))

    async def _chosen(_identity, _chat_id, _selection):
        return (relationship_id, "她")

    monkeypatch.setattr(media, "download_image", _fake_download)
    monkeypatch.setattr(runner, "enqueue_job", _fake_enqueue)
    monkeypatch.setattr(channels, "current", _chosen)

    client = _FakeClient()
    await runner._ingest_image(
        client,
        _image_message(),
        SimpleNamespace(user_id=user_id, claim="c"),
        media.ImageRef(url="https://cdn.example/x"),
    )

    ((job, args),) = queued
    assert job == "ingest_screenshots"
    assert args == (str(user_id), str(relationship_id), "wechat", [b"jpeg bytes"])
    assert "一天内删除" in client.said[0]


async def test_a_provider_failure_answers_instead_of_going_quiet(monkeypatch):
    """The failure the user actually hit: the model returned nothing, the
    exception was logged, and no reply was sent -- so the bot looked like it
    had stopped working rather than like it had failed."""
    from sonder.llm.provider import ProviderError

    async def _boom(*_a, **_kw):
        raise ProviderError("returned no text")

    class _Client(_FakeClient):
        async def send_typing(self, *_a, **_kw):  # pragma: no cover
            return None

        async def get_config(self, *_a, **_kw):  # pragma: no cover
            return SimpleNamespace(typing_ticket=None)

    monkeypatch.setattr(runner.orchestrator, "handle", _boom)
    monkeypatch.setattr(
        runner,
        "_identity_for",
        _returns(SimpleNamespace(user_id=uuid4(), claim="c")),
    )
    monkeypatch.setattr(
        runner,
        "_SELECTION",
        type("S", (), {"get": staticmethod(lambda _c: (uuid4(), "她"))})(),
    )
    monkeypatch.setattr(channels, "current", _chosen_pair())

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _session(_scope):
        yield object()

    monkeypatch.setattr(runner, "dyad_session", _session)

    client = _Client()
    await runner.handle_message(client, _text_message())

    assert client.said, "the user must be told something"
    assert "模型" in client.said[0]


def _returns(value):
    async def _call(*_a, **_kw):
        return value

    return _call


def _chosen_pair():
    async def _call(*_a, **_kw):
        return (uuid4(), "她")

    return _call


def _text_message():
    return SimpleNamespace(
        from_user_id="o9cq@im.wechat",
        context_token="tok",
        item_list=(SimpleNamespace(type=1, text="她刚说她不开心"),),
        has_unhandled_media=False,
        raw={"item_list": [{"type": 1, "text_item": {"text": "她刚说她不开心"}}]},
    )
