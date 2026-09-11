from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from telegram.constants import ChatType

from sonder.bot import telegram


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Session:
    def __init__(self, rows):
        self.rows = rows

    async def execute(self, _query):
        return _Result(self.rows)


def _update(chat_id=7, chat_type=ChatType.PRIVATE, text="hello"):
    message = SimpleNamespace(reply_text=AsyncMock())
    message.text = text
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=9),
        effective_chat=SimpleNamespace(id=chat_id, type=chat_type),
        effective_message=message,
        message=message,
    )


def _identity(user_id=None):
    """An authenticated caller, as `resolve_account` now returns one."""
    from sonder.api.auth import Identity

    return Identity(user_id=user_id or uuid4(), claim="test-claim")


def _user_rows(monkeypatch, rows):
    """Stub the user-scoped session the relationship lookup reads through.

    Patched on `sonder.channels`, not on the bot: the query moved there when
    the three channels were given one implementation, and stubbing the old
    binding would leave the real one running against a database the tests do
    not have.
    """
    from sonder import channels

    @asynccontextmanager
    async def fake_session(scope):
        assert scope.identity_claim, "the bot must present its identity claim"
        yield _Session(rows)

    monkeypatch.setattr(channels, "user_session", fake_session)


async def test_who_duplicate_name_lists_ids_without_changing_selection(monkeypatch):
    user_id = uuid4()
    old_relationship = uuid4()
    candidates = [(uuid4(), "Alex"), (uuid4(), "Alex")]
    update = _update()
    telegram._SELECTION.set(7, (old_relationship, "Old"))
    monkeypatch.setattr(
        telegram, "resolve_account", AsyncMock(return_value=_identity(user_id))
    )
    _user_rows(monkeypatch, candidates)

    await telegram.who(update, SimpleNamespace(args=["Alex"]))

    assert telegram._SELECTION.get(7) == (old_relationship, "Old")
    reply = update.message.reply_text.await_args.args[0]
    assert "1. Alex" in reply and "2. Alex" in reply
    assert str(candidates[0][0]) not in reply, "the UUID is what they cannot supply"


async def test_who_full_relationship_uuid_selects_relationship(monkeypatch):
    relationship_id = uuid4()
    update = _update(chat_id=8)
    monkeypatch.setattr(telegram, "resolve_account", AsyncMock(return_value=_identity()))
    _user_rows(monkeypatch, [(relationship_id, "Alex")])

    await telegram.who(update, SimpleNamespace(args=[str(relationship_id)]))

    assert telegram._SELECTION.get(8) == (relationship_id, "Alex")


async def test_require_context_clears_deleted_relationship(monkeypatch):
    relationship_id = uuid4()
    update = _update(chat_id=10)
    telegram._SELECTION.set(10, (relationship_id, "Alex"))
    monkeypatch.setattr(telegram, "resolve_account", AsyncMock(return_value=_identity()))
    _user_rows(monkeypatch, [])

    assert await telegram.require_context(update) is None
    assert telegram._SELECTION.get(10) is None, "a dead id must not be kept"
    assert "还没有任何对象" in update.message.reply_text.await_args.args[0]


async def test_every_relationship_handler_rejects_group_without_state_access(
    monkeypatch,
):
    secret_name = "Secret Name"
    secret_id = uuid4()
    group_id = -100123
    telegram._SELECTION.set(group_id, (secret_id, secret_name))
    resolve = AsyncMock(side_effect=AssertionError("identity lookup in group"))
    monkeypatch.setattr(telegram, "resolve_account", resolve)

    calls = [
        (telegram.start, _update(group_id, ChatType.GROUP), None),
        (
            telegram.who,
            _update(group_id, ChatType.GROUP),
            SimpleNamespace(args=[secret_name]),
        ),
        (telegram.list_relationships, _update(group_id, ChatType.GROUP), None),
        (
            telegram.on_text,
            _update(group_id, ChatType.GROUP, text="relationship question"),
            SimpleNamespace(),
        ),
        (
            telegram.on_text,
            _update(group_id, ChatType.GROUP, text="/intimate private question"),
            SimpleNamespace(),
        ),
        (telegram.on_photo, _update(group_id, ChatType.GROUP), SimpleNamespace()),
    ]
    action_update = _update(group_id, ChatType.GROUP)
    action_update.callback_query = SimpleNamespace(
        data=f"sent_verbatim:{uuid4()}", answer=AsyncMock()
    )
    calls.append((telegram.on_action, action_update, None))

    for handler, update, context in calls:
        await handler(update, context)
        reply = update.effective_message.reply_text.await_args.args[0]
        assert reply == telegram._PRIVATE_ONLY
        assert secret_name not in reply
        assert str(secret_id) not in reply

    resolve.assert_not_awaited()
    assert telegram._SELECTION.get(group_id) == (secret_id, secret_name)
