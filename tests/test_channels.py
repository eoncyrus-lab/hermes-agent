"""The one implementation three surfaces share.

Telegram, WeChat and the browser page offer the same product, and the failure
mode of three surfaces is three products. What is pinned here is the part that
is now decided once: which relationship a chat is talking about, and what a
result says.

The rendering assertions live here rather than with a channel because the
shape of the message is a product decision. The outcome affordance is not:
Telegram presses a button, WeChat types a digit, and that difference belongs to
the transport.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from sonder import channels
from sonder.storage.scope import Scope


def _rows(*names: str) -> list[tuple]:
    return [(uuid4(), name) for name in names]


# ------------------------------------------------------------- the selection


@pytest.fixture
def store(tmp_path):
    return channels.Selection(tmp_path / "selection.json")


def test_a_selection_survives_a_restart(tmp_path):
    """Losing it means asking again for something the user already answered --
    and what they were asked for was a UUID."""
    path = tmp_path / "selection.json"
    first = channels.Selection(path)
    relationship_id = uuid4()
    first.set("o9cq@im.wechat", (relationship_id, "小美"))

    second = channels.Selection(path)
    assert second.get("o9cq@im.wechat") == (relationship_id, "小美")


def test_an_unreadable_file_is_not_fatal(tmp_path):
    path = tmp_path / "selection.json"
    path.write_text("{ this is not json", encoding="utf-8")
    assert channels.Selection(path).get("anything") is None


def test_a_row_written_by_an_older_version_is_skipped(tmp_path):
    """Hand-edited or from a previous shape. The next message asks again,
    which is cheaper than failing to start."""
    path = tmp_path / "selection.json"
    path.write_text('{"chat": "not-a-pair"}', encoding="utf-8")
    selection = channels.Selection(path)
    assert selection.get("chat") is None


def test_an_unwritable_path_does_not_raise(tmp_path):
    blocked = tmp_path / "nope"
    blocked.mkdir()
    blocked.chmod(0o500)
    try:
        channels.Selection(blocked / "selection.json").set("chat", (uuid4(), "x"))
    finally:
        blocked.chmod(0o700)


# ------------------------------------------------------------- the choosing


def test_a_number_from_the_list_selects():
    rows = _rows("Alex", "Sam")
    assert channels.resolve(rows, "2") == ("ok", rows[1])


def test_a_uuid_still_works():
    rows = _rows("Alex")
    assert channels.resolve(rows, str(rows[0][0])) == ("ok", rows[0])


def test_part_of_a_name_selects():
    rows = _rows("Alexandra", "Sam")
    assert channels.resolve(rows, "lex") == ("ok", rows[0])


def test_an_ambiguous_name_is_ambiguous_not_a_coin_flip():
    rows = _rows("Alex", "Alex")
    kind, payload = channels.resolve(rows, "Alex")
    assert kind == "ambiguous" and len(payload) == 2


def test_an_unknown_name_matches_nothing():
    rows = _rows("Alex")
    kind, payload = channels.resolve(rows, "Nobody")
    assert kind == "nomatch" and payload == rows


def test_no_relationships_is_its_own_answer():
    """Distinct from "no match": one means there is nothing to choose from and
    the other means the user mistyped, and they get different replies."""
    assert channels.resolve([], "Alex")[0] == "none"


def test_the_list_shows_numbers_and_names_and_no_uuid():
    """The UUID is what the old `/who` demanded, and asking somebody to paste
    an identifier they have never seen is the difference between a tool and a
    chore."""
    rows = _rows("Alex", "Sam")
    rendered = channels.numbered(rows)

    assert "1. Alex" in rendered and "2. Sam" in rendered
    for relationship_id, _name in rows:
        assert str(relationship_id) not in rendered


# ------------------------------------------------------- the auto-selection


class _Identity:
    user_id = uuid4()
    claim = "test-claim"


async def test_one_relationship_is_selected_without_asking(monkeypatch, store):
    """There is nothing to disambiguate, and asking anyway costs the user a
    message every time."""
    rows = _rows("Alex")
    monkeypatch.setattr(channels, "relationships", _returning(rows))

    assert await channels.current(_Identity(), "chat", store) == rows[0]
    assert store.get("chat") == rows[0], "and remembered, so it is not re-derived"


async def test_several_relationships_ask(monkeypatch, store):
    monkeypatch.setattr(channels, "relationships", _returning(_rows("Alex", "Sam")))
    assert await channels.current(_Identity(), "chat", store) is None


async def test_a_remembered_selection_wins_over_the_count(monkeypatch, store):
    rows = _rows("Alex", "Sam")
    store.set("chat", rows[1])
    monkeypatch.setattr(channels, "relationships", _returning(rows))
    assert await channels.current(_Identity(), "chat", store) == rows[1]


async def test_a_selection_whose_relationship_is_gone_is_dropped(monkeypatch, store):
    """Dead state is cleared rather than left for the next message to consult,
    and the user is asked again instead of being answered about a person who
    no longer has a record."""
    store.set("chat", (uuid4(), "Deleted"))
    remaining = _rows("Sam")
    monkeypatch.setattr(channels, "relationships", _returning(remaining))

    assert await channels.current(_Identity(), "chat", store) == remaining[0]
    assert store.get("chat") == remaining[0]


async def test_a_selection_is_dropped_even_with_nothing_left(monkeypatch, store):
    """With no relationships at all, every remembered id is stale by
    definition. Returning early here left the entry behind."""
    store.set("chat", (uuid4(), "Deleted"))
    monkeypatch.setattr(channels, "relationships", _returning([]))

    assert await channels.current(_Identity(), "chat", store) is None
    assert store.get("chat") is None


def _returning(rows):
    async def _call(_identity):
        return rows

    return _call


# ------------------------------------------------------------- the wording


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
            rationale="跟她之间的底子还很薄",
            next_milestone="她会主动问你点什么",
        ),
        "health_warning": None,
        "notes": [],
        "rejected_count": 0,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_the_reply_leads_with_the_sentence_and_stays_short():
    """Measured on a real suggestion the old message was 307 characters, of
    which 15 were the thing the user came for."""
    rendered = channels.render_reply(_result(), "hint")
    assert rendered.startswith("在忙什么呢？")
    assert len(rendered) < 80
    assert "— " not in rendered, "the detail block is not in the short reply"


def test_the_hint_is_the_channel_s_own():
    """Pressing a button and typing a digit are not the same instruction."""
    assert "button" in channels.render_reply(_result(), "button")


def test_the_detail_carries_what_the_reply_left_out():
    detail = channels.render_detail(_result())
    assert "建立联系" in detail, "the enum value is not what a person reads"
    assert "establish_rapport" not in detail
    assert "跟她之间的底子还很薄" in detail


def test_an_unknown_objective_prints_its_own_name():
    """A new objective in the enum must not become an empty label."""
    result = _result(
        plan=SimpleNamespace(
            objective=SimpleNamespace(value="brand_new_objective"),
            rationale="r",
            next_milestone="m",
        )
    )
    assert "brand new objective" in channels.render_detail(result)


# ------------------------------------------------- what sonder actually knows


def _state(**groups):
    return SimpleNamespace(as_of=datetime(2026, 9, 11, tzinfo=UTC), **groups)


async def _brief(monkeypatch, state=None, messages=(), trends=()):
    """Run the briefing with the session and the context assembly stubbed out.

    Both are replaced because the assertion is about how a reading is
    *presented*, not about how it was computed -- that arithmetic lives in the
    state engine and is tested there.
    """
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _session(_scope):
        yield object()

    async def _build(_session, scope, **_kw):
        return SimpleNamespace(
            scope=scope,
            state_row=state,
            recent_messages=list(messages),
            trends=list(trends),
            preference=None,
        )

    monkeypatch.setattr(channels, "dyad_session", _session)
    # Patched on the real module rather than on `channels`: the import is
    # local to the function, so there is no `channels.ctx_mod` to reach.
    from sonder.agent import context as ctx_mod

    monkeypatch.setattr(ctx_mod, "build", _build)
    return await channels.briefing(Scope(user_id=uuid4(), relationship_id=uuid4()))


async def test_an_uncertain_reading_is_labelled_not_hidden(monkeypatch):
    """A number presented without its confidence is the confident-sounding
    claim this project exists to refuse."""
    state = _state(
        attraction={"romantic": {"value": 0.29, "confidence": 0.4}},
        intimacy={},
        bond={},
        dynamics={},
        health={},
    )
    out = await _brief(monkeypatch, state=state, messages=[1, 2, 3])
    assert "浪漫吸引" in out
    assert "不确定" in out
    assert "0.29" in out


async def test_a_confident_reading_is_not_hedged(monkeypatch):
    state = _state(
        attraction={},
        intimacy={},
        bond={"trust": {"value": 0.71, "confidence": 0.8}},
        dynamics={},
        health={},
    )
    out = await _brief(monkeypatch, state=state, messages=[1])
    assert "信任" in out
    assert (
        "信任 0.71" in out and "不确定" not in out.split("标了")[0].split("信任")[1][:6]
    )


async def test_an_empty_relationship_says_so_and_asks_for_material(monkeypatch):
    out = await _brief(monkeypatch)
    assert "还什么都没有" in out
    assert "发给我" in out


async def test_an_axis_with_no_label_still_prints(monkeypatch):
    """A new axis in the schema must not vanish from the briefing because
    nobody added it to the label map."""
    state = _state(
        attraction={"brand_new": {"value": 0.5, "confidence": 0.9}},
        intimacy={},
        bond={},
        dynamics={},
        health={},
    )
    out = await _brief(monkeypatch, state=state, messages=[1])
    assert "brand_new" in out
