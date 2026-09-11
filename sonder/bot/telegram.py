"""Telegram client. The first channel, and deliberately not WeChat.

WeChat is where the conversations actually are, but driving a personal WeChat
account through an unofficial protocol risks the account itself. So WeChat is
treated as a source of screenshots the user chooses to share, and Telegram is
where the agent lives.

That constraint turns out to be the better product too: "here is a screenshot
I am showing you" is a different relationship with a tool than "it reads
everything I say", and this tool should be the first kind.
"""

from __future__ import annotations

from datetime import UTC
from uuid import UUID

import structlog
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ChatType
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .. import channels
from ..agent import orchestrator
from ..api.auth import Identity
from ..config import settings
from ..domain.schemas.interaction import Channel
from ..storage.db import dyad_session, user_session
from ..storage.scope import Scope, UserScope

log = structlog.get_logger(__name__)

# Which relationship each chat is about. Shared with the WeChat runner so the
# two channels cannot drift, and persisted so a restart does not make the user
# answer a question they already answered.
_SELECTION = channels.Selection()

# What the short reply left out, so the "why" button answers from what was
# already computed instead of running the models again.
LAST_DETAIL: dict[int, str] = {}

_PRIVATE_ONLY = "这些功能只能在私聊里用。"


def _unlinked_message(telegram_id: int) -> str:
    """Tell the person the one thing the operator needs to link them.

    A bare "not linked" is a dead end: the Telegram user id is not visible to
    the person in the chat, and it is what `sonder telegram link` takes. It is
    logged as well, so the operator can link an account without the person
    having to read a number back to them.
    """
    log.info("unlinked_chat", telegram_id=telegram_id)
    return (
        "这个 Telegram 还没有绑定到任何账号。\n\n"
        f"你的 Telegram ID 是 {telegram_id} —— 把它发给部署这个服务的人，"
        "让他执行绑定。"
    )


# Must match the API's; both write the same column.
ACTIONS = frozenset({"sent_verbatim", "sent_edited", "discarded"})


async def require_private_chat(update: Update) -> bool:
    """Reject relationship work before identity, selection, or storage access."""
    if update.effective_chat.type == ChatType.PRIVATE:
        return True
    await update.effective_message.reply_text(_PRIVATE_ONLY)
    return False


async def resolve_account(telegram_user_id: int) -> Identity | None:
    """Telegram identity -> account. An unlinked chat gets nothing.

    Deliberately a lookup rather than a trusted field: anyone can message a
    bot, and this is the boundary that decides whose relationships they can
    reach.

    Runs on `sonder_identity`, which has no table privileges at all -- it can
    execute this one RPC and nothing else. The bot process therefore never
    holds a credential that could read the mapping table, let alone the
    relationships behind it.
    """
    from sqlalchemy import text

    from ..storage.db import bind_identity, identity_session

    async with identity_session() as session:
        row = (
            await session.execute(
                text("SELECT user_id, identity_claim FROM auth_telegram(:tg)"),
                {"tg": telegram_user_id},
            )
        ).first()
    if row is None:
        return None
    identity = Identity(user_id=row.user_id, claim=row.identity_claim)
    bind_identity(identity.user_id, identity.claim)
    return identity


def _user_scope(identity: Identity) -> UserScope:
    return UserScope(user_id=identity.user_id, identity_claim=identity.claim)


async def require_context(update: Update) -> tuple[Identity, UUID, str] | None:
    """(identity, relationship_id, name) or None, with the reason replied."""
    if not await require_private_chat(update):
        return None
    tg_id = update.effective_user.id
    identity = await resolve_account(tg_id)
    if identity is None:
        await update.effective_message.reply_text(_unlinked_message(tg_id))
        return None
    chat_id = update.effective_chat.id
    # Auto-selects when there is only one, which removes the question entirely
    # for most people. The selection is re-resolved against the database on
    # every message, so a stale or forged entry cannot reach anyone's data.
    chosen = await channels.current(identity, chat_id, _SELECTION)
    if chosen is None:
        rows = await channels.relationships(identity)
        if not rows:
            await update.effective_message.reply_text(
                "你还没有任何对象。先用命令行建一个：\n"
                'sonder relationship create --user <你的 user_id> --name "<化名>"'
            )
        else:
            await update.effective_message.reply_text(
                "这次在说谁？回个数字：\n" + channels.numbered(rows)
            )
        return None
    relationship_id, current_name = chosen
    return identity, relationship_id, current_name


async def start(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private_chat(update):
        return
    # /start is the first thing anyone types, so it is the wrong place to hand
    # back a menu of commands that will all refuse. An unlinked chat gets the
    # one instruction that actually moves it forward.
    if await resolve_account(update.effective_user.id) is None:
        await update.message.reply_text(_unlinked_message(update.effective_user.id))
        return
    await update.message.reply_text(
        "Sonder。\n\n"
        "直接说话就行 —— 把她刚说的话发给我，我给你可以回的几句和理由；"
        "问一句，我给你分析。\n\n"
        "/list — 我都在跟谁　/who — 换一个\n"
        "每个人的数据完全隔离，不会互相串。"
    )


async def who(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Switching people is explicit. Never inferred from message content."""
    if not await require_private_chat(update):
        return
    if not ctx.args:
        await update.message.reply_text("用法：/who 加名字，或者 /list 里的数字")
        return
    identity = await resolve_account(update.effective_user.id)
    if identity is None:
        await update.message.reply_text(_unlinked_message(update.effective_user.id))
        return

    rows = await channels.relationships(identity)
    kind, payload = channels.resolve(rows, " ".join(ctx.args))
    if kind == "none":
        await update.message.reply_text("你还没有任何对象。")
        return
    if kind == "ok":
        _SELECTION.set(update.effective_chat.id, payload)
        await update.message.reply_text(f"好，接下来聊「{payload[1]}」。")
        return
    if kind == "ambiguous":
        await update.message.reply_text(
            "有多个匹配，回数字选一个：\n" + channels.numbered(payload)
        )
        return
    await update.message.reply_text(
        "没找到。回数字选一个：\n" + channels.numbered(payload)
    )


async def list_relationships(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """List the caller's relationship IDs so selection is unambiguous."""
    if not await require_private_chat(update):
        return
    identity = await resolve_account(update.effective_user.id)
    if identity is None:
        await update.message.reply_text(_unlinked_message(update.effective_user.id))
        return

    from sqlalchemy import select

    from ..storage.models import PersonProfileRow, RelationshipRow

    async with user_session(_user_scope(identity)) as session:
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
    if not rows:
        await update.message.reply_text("你还没有任何对象。")
        return
    await update.message.reply_text(
        "用 /who 加名字，或下面的数字：\n"
        + "\n".join(f"{name} — {rid}" for rid, name in rows)
    )


async def on_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private_chat(update):
        return
    resolved = await require_context(update)
    if resolved is None:
        return
    identity, relationship_id, _ = resolved
    user_id = identity.user_id

    await ctx.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    photo = update.message.photo[-1]
    blob = bytes(await (await photo.get_file()).download_as_bytearray())

    from arq.connections import RedisSettings, create_pool

    redis = await create_pool(RedisSettings.from_dsn(settings().redis_url))
    await redis.enqueue_job(
        "ingest_screenshots",
        str(user_id),
        str(relationship_id),
        Channel.WECHAT.value,
        [blob],
    )
    await update.message.reply_text(
        "收到，正在读。\n只保留解析出来的对话，图片本身会在一天内删除。"
    )


def _keyboard(strategy_id: UUID | None) -> InlineKeyboardMarkup | None:
    """Recording what the user did is what makes the learning loop real."""
    if strategy_id is None:
        return None
    sid = str(strategy_id)
    rows = [
        [
            InlineKeyboardButton("发出去了", callback_data=f"sent_verbatim:{sid}"),
            InlineKeyboardButton("改了", callback_data=f"sent_edited:{sid}"),
            InlineKeyboardButton("没发", callback_data=f"discarded:{sid}"),
        ]
    ]
    # On its own row so it is not mistaken for a fourth outcome. WeChat has no
    # buttons and asks for the word instead; this is the same question with
    # the affordance the transport actually has.
    if sid:
        rows.append([InlineKeyboardButton("为什么", callback_data="why")])
    return InlineKeyboardMarkup(rows)


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private_chat(update):
        return
    resolved = await require_context(update)
    if resolved is None:
        return
    identity, relationship_id, name = resolved
    user_id = identity.user_id
    scope = Scope(user_id, relationship_id, identity.claim)

    await ctx.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    async with dyad_session(scope) as session:
        result = await orchestrator.handle(session, scope, user_id, update.message.text)

    if isinstance(result, str):
        await update.message.reply_text(result[:4000])
        return

    if result.health_warning:
        await update.message.reply_text(result.health_warning)

    if not result.suggestions:
        await update.message.reply_text(
            "这次没有我会建议你发的。" + ("；".join(result.notes) if result.notes else "")
        )
        return

    head = result.suggestions[0]
    # The sentence, and one line of affordance. Everything else -- objective,
    # rationale, milestone, notes, alternatives -- is behind the button. On a
    # phone this used to be a wall to scroll past before reaching the one line
    # the user came for.
    if head.strategy_id is not None:
        LAST_DETAIL[update.effective_chat.id] = channels.render_detail(result)
    await update.message.reply_text(
        channels.render_reply(result, "记录一下："),
        reply_markup=_keyboard(head.strategy_id),
    )


async def on_action(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private_chat(update):
        return
    query = update.callback_query
    await query.answer()
    action, _, sid = query.data.partition(":")

    # Not an outcome: the reasoning behind the suggestion that is still on
    # screen, answered from what was already computed.
    if action == "why":
        detail = LAST_DETAIL.get(update.effective_chat.id)
        await query.edit_message_reply_markup(reply_markup=None)
        if detail:
            await update.effective_message.reply_text(detail)
        return

    # Callback data is attacker-supplied: Telegram will deliver whatever a
    # client puts there. The API validates this same field, and the column has
    # no CHECK behind it, so an unknown action would otherwise be written
    # straight into the outcome record the learning loop reads.
    if action not in ACTIONS:
        return
    try:
        strategy_id = UUID(sid)
    except ValueError:
        return
    resolved = await require_context(update)
    if resolved is None:
        return
    identity, relationship_id, _n = resolved
    scope = Scope(identity.user_id, relationship_id, identity.claim)

    from datetime import datetime

    from sqlalchemy import update as sql_update

    from ..storage.models import StrategyRecordRow

    async with dyad_session(scope) as session:
        await session.execute(
            sql_update(StrategyRecordRow)
            .where(
                StrategyRecordRow.id == strategy_id,
                StrategyRecordRow.relationship_id == scope.relationship_id,
            )
            .values(
                user_action=action,
                sent_at=datetime.now(UTC) if action.startswith("sent") else None,
            )
        )
    await query.edit_message_reply_markup(reply_markup=None)


def build() -> Application:
    token = settings().telegram_token
    if not token:
        raise RuntimeError("SONDER_TELEGRAM_TOKEN is not set")
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("who", who))
    app.add_handler(CommandHandler("list", list_relationships))
    app.add_handler(CommandHandler("intimate", on_text))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(CallbackQueryHandler(on_action))
    return app


def main() -> None:
    build().run_polling()


if __name__ == "__main__":
    main()
