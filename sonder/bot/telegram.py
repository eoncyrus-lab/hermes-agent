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

from ..agent import orchestrator
from ..api.auth import Identity
from ..config import settings
from ..domain.schemas.interaction import Channel
from ..storage.db import dyad_session, user_session
from ..storage.scope import Scope, UserScope

log = structlog.get_logger(__name__)

# Which relationship a chat is currently talking about. Selection is explicit
# and per-chat; the account behind it comes from the database, never from here.
SELECTED: dict[int, tuple[UUID, str]] = {}

_PRIVATE_ONLY = "Sonder relationship features are only available in a private chat."

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
        await update.effective_message.reply_text(
            "This chat is not linked to an account."
        )
        return None
    chosen = SELECTED.get(update.effective_chat.id)
    if chosen is None:
        await update.effective_message.reply_text(
            "Tell me who this is about first: /who <relationship UUID>"
        )
        return None
    relationship_id, name = chosen

    # The selection lives in memory; confirm against the database every time
    # so a stale or forged selection cannot reach someone else's data.
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
                    RelationshipRow.id == relationship_id,
                    RelationshipRow.user_id == identity.user_id,
                )
            )
        ).all()
    if not rows:
        SELECTED.pop(update.effective_chat.id, None)
        await update.effective_message.reply_text(
            "That relationship is no longer available. Choose another with /list."
        )
        return None
    relationship_id, current_name = rows[0]
    if current_name != name:
        SELECTED[update.effective_chat.id] = (relationship_id, current_name)
    return identity, relationship_id, current_name


async def start(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private_chat(update):
        return
    await update.message.reply_text(
        "Sonder.\n\n"
        "/who <relationship UUID> — choose who we're talking about\n"
        "/list — who I'm tracking\n"
        "Send a screenshot to add it to that conversation.\n"
        "Send a message to ask for a reply, a read, or a review.\n\n"
        "Everything is kept per person and never crosses between them."
    )


async def who(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Switching people is explicit. Never inferred from message content."""
    if not await require_private_chat(update):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /who <relationship UUID> (or exact name)")
        return
    identity = await resolve_account(update.effective_user.id)
    if identity is None:
        await update.message.reply_text("This chat is not linked to an account.")
        return

    from sqlalchemy import select

    from ..storage.models import PersonProfileRow, RelationshipRow

    value = " ".join(ctx.args)
    try:
        relationship_id = UUID(value)
    except ValueError:
        relationship_id = None

    query = (
        select(RelationshipRow.id, PersonProfileRow.display_name)
        .join(PersonProfileRow, PersonProfileRow.id == RelationshipRow.target_person_id)
        .where(
            RelationshipRow.user_id == identity.user_id,
            PersonProfileRow.subject == "target",
        )
    )
    if relationship_id is not None:
        query = query.where(RelationshipRow.id == relationship_id)
    else:
        query = query.where(PersonProfileRow.display_name == value)
    async with user_session(_user_scope(identity)) as session:
        matches = (await session.execute(query)).all()
    if not matches:
        await update.message.reply_text(f"No relationship matches {value}.")
        return
    if len(matches) > 1:
        candidates = "\n".join(f"{name} — {rid}" for rid, name in matches)
        await update.message.reply_text(
            "That name is ambiguous. Choose with the full relationship UUID:\n"
            + candidates
        )
        return
    relationship_id, name = matches[0]
    SELECTED[update.effective_chat.id] = (relationship_id, name)
    await update.message.reply_text(
        f"Now talking about {name}. Everything from here is scoped to them."
    )


async def list_relationships(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """List the caller's relationship IDs so selection is unambiguous."""
    if not await require_private_chat(update):
        return
    identity = await resolve_account(update.effective_user.id)
    if identity is None:
        await update.message.reply_text("This chat is not linked to an account.")
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
        await update.message.reply_text("You have no relationships to choose from.")
        return
    await update.message.reply_text(
        "Choose one with /who <relationship UUID>:\n"
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
        "Got it — reading it now. I'll only keep the extracted conversation; "
        "the image itself is deleted within a day."
    )


def _keyboard(strategy_id: UUID | None) -> InlineKeyboardMarkup | None:
    """Recording what the user did is what makes the learning loop real."""
    if strategy_id is None:
        return None
    sid = str(strategy_id)
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("sent it", callback_data=f"sent_verbatim:{sid}"),
                InlineKeyboardButton("edited", callback_data=f"sent_edited:{sid}"),
                InlineKeyboardButton("skipped", callback_data=f"discarded:{sid}"),
            ]
        ]
    )


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
            "Nothing I'd suggest sending here. "
            + ("; ".join(result.notes) if result.notes else "")
        )
        return

    head = result.suggestions[0]
    lines = [
        head.text,
        "",
        f"— aiming for: {result.plan.objective.value.replace('_', ' ')}",
        f"— {result.plan.rationale}",
    ]
    if result.plan.next_milestone:
        lines.append(f"— progress would look like: {result.plan.next_milestone}")
    for note in result.notes:
        lines.append(f"— {note}")
    if len(result.suggestions) > 1:
        lines.append("")
        lines.append("alternatives:")
        for alt in result.suggestions[1:]:
            lines.append(f"  ({alt.register}) {alt.text}")

    await update.message.reply_text(
        "\n".join(lines), reply_markup=_keyboard(head.strategy_id)
    )


async def on_action(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private_chat(update):
        return
    query = update.callback_query
    await query.answer()
    action, _, sid = query.data.partition(":")
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
