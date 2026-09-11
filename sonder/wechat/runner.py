"""The WeChat channel: Tencent's ClawBot, over the iLink Bot API.

The shape is the Telegram bot's, deliberately. A chat selects one
relationship, plain text goes to `orchestrator.handle`, and what comes back is
either a considered answer or a set of reply candidates with the reasoning
behind them. The transport differs; the product must not.

Three things are specific to this channel:

* **`context_token`.** Every inbound message carries one, and a reply has to
  send it back verbatim or the message lands in a different window.
  `ILinkClient.reply` takes the whole inbound message for that reason, so a
  caller cannot pair a sender with the wrong token.
* **No inline buttons.** The iLink item types are text, image, voice, file and
  video -- there is nothing to press. Telegram records an outcome with three
  buttons; here it is `/sent`, `/edited` and `/skipped`, acting on the most
  recent suggestion in that chat.
* **A durable cursor.** `getupdates` is a long poll over an opaque cursor that
  must be carried forward. Held only in memory, a restart would replay the
  backlog, and the bot would answer messages it has already answered. So it is
  written to disk after every poll, the way the official client does it.
"""

from __future__ import annotations

import asyncio
from uuid import UUID

import structlog

from .. import channels
from ..agent import orchestrator
from ..api.auth import Identity
from ..api.queue import enqueue as enqueue_job
from ..config import REPO_ROOT, settings
from ..domain.schemas.interaction import Channel
from ..llm.provider import ProviderError
from ..storage.db import dyad_session, identity_session, user_session
from ..storage.scope import Scope, UserScope
from . import media
from .client import (
    ILinkClient,
    ILinkError,
    ILinkStaleToken,
    WeixinMessage,
    extract_text,
)

log = structlog.get_logger(__name__)

# Which relationship each chat is about. A per-chat affordance rather than a
# record, shared with the Telegram bot so the two channels cannot drift.
_SELECTION = channels.Selection()

# The last suggestion offered in a chat, so an outcome has something to record
# against. In-memory on purpose: it is an affordance, not a fact, and a
# restarted process simply has nothing to attribute.
LAST_STRATEGY: dict[str, UUID] = {}

# What the short reply left out, kept so "为什么" answers from what was already
# computed rather than by running the models again.
LAST_DETAIL: dict[str, str] = {}

# Whole-message matches only. "为什么" inside a sentence is a question about
# her, not a request for the rationale, and answering the wrong one would be
# worse than not answering.
WHY_WORDS = frozenset(
    {"为什么", "为啥", "理由", "详细", "为什么这样", "别的", "其他", "其他说法", "why"}
)

ACTIONS = {"sent_verbatim": "发出去了", "sent_edited": "改了再发", "discarded": "没发"}
COMMANDS = {"sent": "sent_verbatim", "edited": "sent_edited", "skipped": "discarded"}
# The same three outcomes as one keystroke. Slash commands are fine to type
# once; this is asked after every suggestion, and the difference between
# "/sent" and "1" is the difference between the loop being used and skipped.
DIGIT_OUTCOMES = {"1": "sent_verbatim", "2": "sent_edited", "3": "discarded"}

_CURSOR_FILE = REPO_ROOT / "storage" / "wechat-cursor"


def _load_cursor() -> str:
    try:
        return _CURSOR_FILE.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""
    except OSError as exc:  # unreadable is not fatal, just lossy
        log.warning("wechat_cursor_unreadable", error=str(exc))
        return ""


def _save_cursor(cursor: str) -> None:
    """Best effort, and deliberately not fatal.

    Losing the cursor means the next start replays the backlog, which is
    annoying but converges: ingest is content-addressed, so a redelivered
    conversation deduplicates. Refusing to run because a state file could not
    be written would be worse than the replay.
    """
    try:
        _CURSOR_FILE.parent.mkdir(parents=True, exist_ok=True)
        _CURSOR_FILE.write_text(cursor, encoding="utf-8")
    except OSError as exc:
        log.warning("wechat_cursor_unwritable", error=str(exc))


async def resolve_account(wechat_user_id: str) -> Identity | None:
    """WeChat identity -> account, or nothing.

    Anyone can message a bot, so this lookup is the boundary that decides whose
    relationships a sender can reach. It runs on `sonder_wechat`, which holds
    no table privileges at all -- it can execute `auth_wechat` and nothing
    else, so this process never carries a credential that could read the
    mapping table, let alone the relationships behind it.
    """
    from sqlalchemy import text

    async with identity_session() as session:
        row = (
            await session.execute(
                text("SELECT user_id, identity_claim FROM auth_wechat(:w)"),
                {"w": wechat_user_id},
            )
        ).first()
    if row is None:
        return None
    return Identity(user_id=row.user_id, claim=row.identity_claim)


def _unlinked_message(wechat_user_id: str) -> str:
    """The one instruction that actually moves this forward.

    A bare "not linked" is a dead end: the WeChat handle is not something the
    person can read off their own screen, and it is what `sonder wechat link`
    takes. It is logged too, so the operator can link the account without
    asking anyone to read a string back to them.
    """
    log.info("unlinked_wechat_chat", wechat_user_id=wechat_user_id)
    return (
        "这个微信还没有绑定到任何账号。\n\n"
        f"你的微信标识是：\n{wechat_user_id}\n\n"
        "把它发给部署这个服务的人，让他执行绑定。"
    )


async def _identity_for(message: WeixinMessage) -> Identity | None:
    return await resolve_account(message.from_user_id)


# What the channel can do with the suggestion, in the only vocabulary it has.
# Telegram presses a button here; this is the same instruction typed.
HINT = "1发了 2改了 3没发 ·「为什么」看理由"


async def _list(identity: Identity) -> str:
    rows = await channels.relationships(identity)
    if not rows:
        return "你还没有任何对象。用命令行建一个：sonder relationship create"
    if len(rows) == 1:
        return f"就一个：「{rows[0][1]}」。直接说话就行。"
    return "用 /who 加名字，或下面的数字：\n" + channels.numbered(rows)


async def _who(identity: Identity, chat_id: str, argument: str) -> str:
    """Select the relationship this chat is about.

    The decision is `channels.resolve`; only the words are this channel's.
    """
    rows = await channels.relationships(identity)
    kind, payload = channels.resolve(rows, argument)
    if kind == "none":
        return "你还没有任何对象。用命令行建一个：sonder relationship create"
    if kind == "ok":
        _SELECTION.set(chat_id, payload)
        return f"好，接下来聊「{payload[1]}」。"
    if kind == "ambiguous":
        return "有多个匹配，回数字选一个：\n" + channels.numbered(payload)
    return "没找到这个对象。回数字选一个：\n" + channels.numbered(payload)


async def _record(identity: Identity, chat_id: str, action: str) -> str:
    """Record what the user actually did. This is what makes the loop real."""
    strategy_id = LAST_STRATEGY.get(chat_id)
    if strategy_id is None:
        return "这个对话里还没有可以记录的建议。"

    from sqlalchemy import select, update

    from ..storage.models import StrategyRecordRow

    scope = UserScope(user_id=identity.user_id, identity_claim=identity.claim)
    async with user_session(scope) as session:
        owned = (
            await session.execute(
                select(StrategyRecordRow.id).where(StrategyRecordRow.id == strategy_id)
            )
        ).scalar()
        if owned is None:
            return "这条建议已经不在记录里了。"
        await session.execute(
            update(StrategyRecordRow)
            .where(StrategyRecordRow.id == strategy_id)
            .values(user_action=action)
        )
    LAST_STRATEGY.pop(chat_id, None)
    # The rationale belonged to a suggestion that has now been acted on;
    # "为什么" after that would be a question about the record, not about a
    # message still on screen.
    LAST_DETAIL.pop(chat_id, None)
    return f"记下了：{ACTIONS[action]}。"


async def handle_message(client: ILinkClient, message: WeixinMessage) -> None:
    chat_id = message.from_user_id
    body = extract_text(message).strip()

    identity = await _identity_for(message)
    if identity is None:
        await client.reply(message, _unlinked_message(chat_id))
        return

    # A forwarded screenshot is the obvious thing to send this channel, and it
    # arrives with no text at all. Handled before the command parse, because
    # there is nothing to parse.
    image = media.image_reference(message)
    if image is not None:
        await _ingest_image(client, message, identity, image)
        return

    if message.has_unhandled_media and not body:
        # Something arrived that this channel cannot read yet -- a voice note,
        # a file, a video. Saying so is the whole of the handling: silently
        # ignoring it would look like sonder had read it and had nothing to say.
        await client.reply(
            message,
            "这个类型的附件我还读不了（目前支持图片和文字）。\n"
            "把文字复制过来发我也可以。",
        )
        return

    command, _, argument = body.partition(" ")
    command = command.lower()

    if command in ("/start", "/help"):
        await client.reply(
            message,
            "Sonder。\n\n"
            "直接说话就行 —— 把她刚说的话发给我，我给你可以回的几句和理由；"
            "问一句，我给你分析。\n\n"
            "发出去之后回 1、改了回 2、没发回 3，我就知道下次该怎么调。\n\n"
            "/list — 我都在跟谁　/who — 换一个\n"
            "每个人的数据完全隔离，不会互相串。",
        )
        return
    if command == "/who":
        await client.reply(message, await _who(identity, chat_id, argument))
        return
    if command == "/list":
        await client.reply(message, await _list(identity))
        return
    if command in COMMANDS:
        await client.reply(message, await _record(identity, chat_id, COMMANDS[command]))
        return
    # A bare digit is the outcome of the suggestion just offered. Gated on
    # there being one outstanding, so "1" in any other context is just a "1".
    if body in DIGIT_OUTCOMES and chat_id in LAST_STRATEGY:
        await client.reply(
            message, await _record(identity, chat_id, DIGIT_OUTCOMES[body])
        )
        return
    # The same gating for "why": it only means "explain that suggestion" while
    # there is a suggestion on screen to explain.
    detail = LAST_DETAIL.get(chat_id)
    if detail and body.strip().lower() in WHY_WORDS:
        await client.reply(message, detail)
        return
    if body.strip() in channels.BRIEF_WORDS:
        chosen = await channels.current(identity, chat_id, _SELECTION)
        if chosen is None:
            await client.reply(message, await _list(identity))
            return
        relationship_id, name = chosen
        scope = Scope(
            user_id=identity.user_id,
            relationship_id=relationship_id,
            identity_claim=identity.claim,
        )
        await client.reply(
            message, f"关于「{name}」：\n\n" + await channels.briefing(scope)
        )
        return

    chosen = await channels.current(identity, chat_id, _SELECTION)
    if chosen is None:
        rows = await channels.relationships(identity)
        if not rows:
            await client.reply(
                message,
                "你还没有任何对象。先用命令行建一个：\n"
                'sonder relationship create --user <你的 user_id> --name "<化名>"',
            )
        else:
            await client.reply(
                message, "这次在说谁？回个数字：\n" + channels.numbered(rows)
            )
        return
    relationship_id, _name = chosen

    scope = Scope(
        user_id=identity.user_id,
        relationship_id=relationship_id,
        identity_claim=identity.claim,
    )

    # Best effort, and asked for at the last moment: a typing indicator that
    # outlives the answer, or that fails and takes the answer with it, is worse
    # than none. The ticket is per-conversation and comes from getconfig.
    try:
        config = await client.get_config(chat_id, message.context_token)
        if config.typing_ticket:
            await client.send_typing(chat_id, config.typing_ticket)
    except ILinkError as exc:
        log.debug("wechat_typing_unavailable", error=str(exc))

    try:
        async with dyad_session(scope) as session:
            result = await orchestrator.handle(session, scope, identity.user_id, body)
    except ProviderError as exc:
        # A model that returned nothing is not the user's problem to debug,
        # but it is their problem to wait on. Saying so beats the alternative,
        # which was logging it and sending no reply at all.
        log.warning("wechat_provider_failed", error=str(exc))
        await client.reply(message, channels.PROVIDER_FAILED)
        return

    if isinstance(result, str):
        await client.reply(message, result[:4000])
        return

    if result.health_warning:
        await client.reply(message, f"⚠ {result.health_warning}")

    if not result.suggestions:
        notes = "；".join(result.notes) if result.notes else ""
        await client.reply(message, f"这次没有我会建议你发的。{notes}")
        return

    head = result.suggestions[0]
    if head.strategy_id is not None:
        LAST_STRATEGY[chat_id] = head.strategy_id
    # Kept so the follow-up question is answered from what was already
    # computed rather than by running the models again.
    LAST_DETAIL[chat_id] = channels.render_detail(result)
    await client.reply(message, channels.render_reply(result, HINT))


async def _ingest_image(
    client: ILinkClient,
    message: WeixinMessage,
    identity: Identity,
    image: media.ImageRef,
) -> None:
    """Download a forwarded picture and hand it to the existing ingest path.

    It goes through `ingest_screenshots`, unchanged: a picture arriving over
    WeChat and a picture arriving over the API are the same observation, and
    the vision model, the evidence refs and the artifact TTL should not care
    which door it came through.

    The relationship has to have been resolved first. A picture with nowhere to
    file it is not something to guess at -- guessing would write a real
    person's words into the wrong person's record.
    """
    chosen = await channels.current(identity, message.from_user_id, _SELECTION)
    if chosen is None:
        rows = await channels.relationships(identity)
        if not rows:
            await client.reply(
                message,
                "你还没有任何对象。先用命令行建一个：\n"
                'sonder relationship create --user <你的 user_id> --name "<化名>"',
            )
        else:
            await client.reply(
                message, "这张图说的是谁？回个数字：\n" + channels.numbered(rows)
            )
        return

    relationship_id, _name = chosen
    try:
        blob = await media.download_image(image)
    except media.MediaError as exc:
        # Named, not swallowed: a key that fails to decrypt and a CDN that
        # refused the fetch are different problems for whoever reads the log.
        log.warning(
            "wechat_image_failed",
            error=str(exc),
            # The shape of what arrived, never the key: the useful question is
            # which field it came from and how long it is.
            item=media.describe_image(message),
        )
        await client.reply(
            message, "这张图我拿不到（下载或解密失败），可以再发一次试试。"
        )
        return

    await enqueue_job(
        "ingest_screenshots",
        str(identity.user_id),
        str(relationship_id),
        Channel.WECHAT.value,
        [blob],
    )
    await client.reply(
        message,
        "收到，正在读。\n"
        "只保留解析出来的对话，图片本身会在一天内删除。\n"
        "读完回一句「分析一下」就能看到状态。",
    )


async def run() -> None:
    """Long-poll until stopped, reconnecting rather than exiting.

    A stale token is the one failure that will not fix itself -- it needs a new
    QR scan -- so it is logged and retried on a slow loop instead of killing
    the process, which would leave a supervisor restarting into the same wall.
    """
    token = settings().wechat_bot_token
    if not token:
        raise RuntimeError(
            "SONDER_WECHAT_BOT_TOKEN is not set; run `sonder wechat login` first"
        )

    cursor = _load_cursor()
    client = ILinkClient(token, settings().wechat_base_url)
    log.info("wechat_channel_starting", base_url=client.base_url)
    try:
        await client.notify_start()
    except ILinkError as exc:
        log.warning("wechat_notify_start_failed", error=str(exc))

    try:
        while True:
            try:
                page = await client.get_updates(cursor)
            except ILinkStaleToken:
                log.error("wechat_token_stale", hint="run `sonder wechat login` again")
                await asyncio.sleep(60)
                continue
            except ILinkError as exc:
                log.warning("wechat_poll_failed", error=str(exc))
                await asyncio.sleep(5)
                continue

            if page.cursor and page.cursor != cursor:
                cursor = page.cursor
                _save_cursor(cursor)

            for message in page.messages:
                try:
                    await handle_message(client, message)
                except Exception as exc:
                    # One bad message must not take the channel down with it.
                    log.exception("wechat_message_failed", error=str(exc))
    finally:
        try:
            await client.notify_stop()
        except ILinkError:
            log.debug("wechat_notify_stop_failed")
        await client.aclose()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
