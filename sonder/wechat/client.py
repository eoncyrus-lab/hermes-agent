"""Tencent iLink Bot API client: the official WeChat bot channel.

This is the one channel here that is not a workaround. `ilinkai.weixin.qq.com`
is Tencent's own bot gateway, the bot authenticates with a token it was paired
for, and nothing in this module drives a personal WeChat account. That is what
lets it exist at all, given that `sonder/bot/telegram.py` is where the agent
lives precisely because an unofficial WeChat client puts the account itself at
risk.

Four properties of the protocol shaped the code:

  * Failures arrive as HTTP 200 with `ret` set. Every response is checked
    before it is handed to a caller, because a reply the gateway refused and a
    reply it delivered look identical otherwise.
  * `getupdates` is a long poll the server holds for up to 35 s, and a
    client-side read timeout is how an empty poll ends. That is normal control
    flow here, not an error.
  * `context_token` is per conversation and must be echoed verbatim. It decides
    which chat window a reply lands in and it is not derivable from the user
    id, so it travels with the message instead of being cached per user.
  * Item types grow: Tencent has already added tool-call frames (11 and 12).
    Parsing therefore never rejects an item for being unrecognised, and never
    lets an unrecognised item contribute text.

Verified against @tencent-weixin/openclaw-weixin 2.4.8 -- `src/api/types.ts`
for the wire schema, `src/api/api.ts` for headers, timeouts and error handling,
`src/auth/login-qr.ts` for the pairing flow, and `src/monitor/monitor.ts` for
the long-poll loop and its cursor handling.
"""

from __future__ import annotations

import asyncio
import base64
import secrets
import time
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Final

import httpx
import structlog

log = structlog.get_logger(__name__)

DEFAULT_BASE_URL: Final = "https://ilinkai.weixin.qq.com"
"""The production bot gateway. A pairing may name a different host for the token."""

DEFAULT_CHANNEL_VERSION: Final = "2.4.8"
"""Client build reported in `base_info`. The release this module was written against."""

AUTHORIZATION_TYPE: Final = "ilink_bot_token"
"""Value of the `AuthorizationType` header. Not a scheme the token is prefixed with."""

DEFAULT_BOT_TYPE: Final = "3"
"""`bot_type` for the QR endpoints: the bot channel this client speaks."""

LONGPOLL_TIMEOUT_S: Final = 35.0
"""How long the server holds `getupdates`. The response may suggest a different value."""

SEND_TIMEOUT_S: Final = 15.0
"""Deliveries: `sendmessage` and `getuploadurl`."""

CONFIG_TIMEOUT_S: Final = 10.0
"""Lightweight calls: `getconfig`, `sendtyping`, and the listener start/stop notices."""

DEFAULT_TIMEOUT_S: Final = SEND_TIMEOUT_S
"""Timeout for a client built here; every call overrides it with its own."""

QR_POLL_TIMEOUT_S: Final = LONGPOLL_TIMEOUT_S
"""`get_qrcode_status` long-polls too, so it gets the same budget as `getupdates`."""

LOGIN_TIMEOUT_S: Final = 480.0
"""How long a human gets to scan before the pairing attempt is abandoned."""

POLL_INTERVAL_S: Final = 1.0
"""Pause between pairing polls, matching the official client's cadence."""

STALE_TOKEN_ERRCODE: Final = -14
"""Reported as `errcode` or `ret` when the bot token is no longer valid."""

LOGIN_CONFIRMED: Final = "confirmed"
"""The one pairing status that carries a token."""

LOGIN_WAIT: Final = "wait"
"""The not-scanned-yet status, and what an unreadable poll is reported as."""

# Item types of `item_list[]`. Raw ints rather than an enum: an enum invites
# `ItemType(raw)` at the parse boundary, which would turn the next type Tencent
# ships into a failure to read a message at all.
ITEM_NONE: Final = 0
ITEM_TEXT: Final = 1
ITEM_IMAGE: Final = 2
ITEM_VOICE: Final = 3
ITEM_FILE: Final = 4
ITEM_VIDEO: Final = 5
ITEM_TOOL_CALL_START: Final = 11
ITEM_TOOL_CALL_RESULT: Final = 12

MEDIA_ITEM_TYPES: Final[frozenset[int]] = frozenset(
    {ITEM_IMAGE, ITEM_VOICE, ITEM_FILE, ITEM_VIDEO}
)
"""Item types that carry a payload this client does not download."""

KNOWN_ITEM_TYPES: Final[frozenset[int]] = frozenset(
    {ITEM_NONE, ITEM_TEXT, ITEM_TOOL_CALL_START, ITEM_TOOL_CALL_RESULT} | MEDIA_ITEM_TYPES
)
"""Every type this module models. Anything absent is reported, not rejected."""

MESSAGE_TYPE_NONE: Final = 0
MESSAGE_TYPE_USER: Final = 1
"""A message the human sent."""
MESSAGE_TYPE_BOT: Final = 2
"""A message this bot sent, which `getupdates` may echo back."""

MESSAGE_STATE_NEW: Final = 0
MESSAGE_STATE_GENERATING: Final = 1
MESSAGE_STATE_FINISH: Final = 2
"""Complete. Outbound text is sent in this state."""

TYPING_STATUS_TYPING: Final = 1
TYPING_STATUS_CANCEL: Final = 2

# `UploadMediaType`, a different namespace from the item types above even though
# it is the same document: video is 2 here and 5 there. Two sets of names beat
# one clever mapping between them.
UPLOAD_MEDIA_IMAGE: Final = 1
UPLOAD_MEDIA_VIDEO: Final = 2
UPLOAD_MEDIA_FILE: Final = 3
UPLOAD_MEDIA_VOICE: Final = 4

_ENDPOINT_QRCODE: Final = "ilink/bot/get_bot_qrcode"
_ENDPOINT_QRCODE_STATUS: Final = "ilink/bot/get_qrcode_status"
_ENDPOINT_GET_UPDATES: Final = "ilink/bot/getupdates"
_ENDPOINT_SEND_MESSAGE: Final = "ilink/bot/sendmessage"
_ENDPOINT_GET_CONFIG: Final = "ilink/bot/getconfig"
_ENDPOINT_SEND_TYPING: Final = "ilink/bot/sendtyping"
_ENDPOINT_GET_UPLOAD_URL: Final = "ilink/bot/getuploadurl"
_ENDPOINT_NOTIFY_START: Final = "ilink/bot/msg/notifystart"
_ENDPOINT_NOTIFY_STOP: Final = "ilink/bot/msg/notifystop"


class ILinkError(RuntimeError):
    """Base for everything this module raises.

    A dedicated root means a caller can wrap the whole channel in one `except`
    without swallowing its own bugs along with it.
    """


class ILinkAPIError(ILinkError):
    """The gateway answered and refused.

    HTTP 200 with a non-zero `ret` is how this API reports failure, rejected
    deliveries included, so the protocol's own codes are carried rather than
    paraphrased and the message names the endpoint that failed.
    """

    def __init__(
        self,
        message: str,
        *,
        endpoint: str,
        ret: int | None = None,
        errcode: int | None = None,
        errmsg: str = "",
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.endpoint = endpoint
        self.ret = ret
        self.errcode = errcode
        self.errmsg = errmsg
        self.status_code = status_code


class ILinkStaleToken(ILinkAPIError):
    """The token is dead, and retrying will not change that.

    The gateway reports this as errcode -14 and the official client's response
    is to stop calling for an hour and expect a re-pair. Separated from its
    parent so a polling loop can ask a human for a new pairing instead of
    hammering an endpoint that will refuse every request.
    """


class ILinkTransportError(ILinkError):
    """No usable answer arrived: DNS, TLS, connection, or a timeout."""

    def __init__(self, message: str, *, endpoint: str, timed_out: bool = False) -> None:
        super().__init__(message)
        self.endpoint = endpoint
        self.timed_out = timed_out


class ILinkProtocolError(ILinkError):
    """The answer was not the documented shape.

    Distinct from `ILinkAPIError` because the causes are different: a non-JSON
    body or a missing field means a wrong base URL, a captive portal, or a
    gateway error page, and repeating the request will not fix any of them.
    """


class ILinkLoginTimeout(ILinkError):
    """Nobody scanned the pairing code before the window closed."""


@dataclass(frozen=True, slots=True)
class BaseInfo:
    """Request metadata the official client attaches to every call.

    `channel_version` is the client build being spoken for. `bot_agent` is an
    observability hint only -- upstream sanitises it into a User-Agent-like
    string and never authenticates with it -- so leaving it None omits the key
    rather than asserting an identity this client does not have.
    """

    channel_version: str = DEFAULT_CHANNEL_VERSION
    bot_agent: str | None = None

    def to_wire(self) -> dict[str, Any]:
        """The object for the `base_info` key of a request."""
        payload: dict[str, Any] = {"channel_version": self.channel_version}
        if self.bot_agent is not None:
            payload["bot_agent"] = self.bot_agent
        return payload


@dataclass(frozen=True, slots=True)
class MessageItem:
    """One entry of a message's `item_list`.

    `type` keeps the raw wire number. `text` holds whatever the item
    contributes to the conversation: `text_item.text` for a text item, and the
    server's own transcript for a voice item. It stays None for everything
    else, an untranscribed voice note included.
    """

    type: int = ITEM_NONE
    text: str | None = None

    @property
    def is_text(self) -> bool:
        """True for a typed text item."""
        return self.type == ITEM_TEXT

    @property
    def is_media(self) -> bool:
        """True for image, voice, file or video, transcript or not."""
        return self.type in MEDIA_ITEM_TYPES

    @property
    def is_unhandled_media(self) -> bool:
        """Media whose payload this client never looks at.

        A voice note that arrived with a transcript is not unhandled: the words
        are already in `text`, which is what the caller reads.
        """
        return self.is_media and self.text is None

    @property
    def is_known(self) -> bool:
        """False for a type this module does not model yet."""
        return self.type in KNOWN_ITEM_TYPES


@dataclass(frozen=True, slots=True)
class WeixinMessage:
    """One message from `getupdates`, inbound or an echo of our own outbound.

    Every field is optional on the wire, so every field here has a default: a
    malformed message parses into something a caller can inspect instead of
    taking its whole batch down with it. Ids default to empty strings rather
    than None so a caller forwarding `from_user_id` has one shape of "no
    sender" to handle.

    `raw` is the message as it arrived, for fields this module does not model.
    Nothing above is derived from it lazily, so a caller should not reach into
    it for anything already exposed as a field.
    """

    from_user_id: str = ""
    to_user_id: str = ""
    message_type: int = MESSAGE_TYPE_NONE
    context_token: str = ""
    item_list: tuple[MessageItem, ...] = ()
    message_id: int | None = None
    seq: int | None = None
    session_id: str | None = None
    client_id: str | None = None
    run_id: str | None = None
    create_time_ms: int | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)

    @property
    def is_from_user(self) -> bool:
        """True when a human sent this, rather than this bot (type 2).

        The server only pushes user messages today, so this is not a filter the
        loop has to apply -- it is the assertion that keeps that assumption
        visible if it ever stops holding.
        """
        return self.message_type == MESSAGE_TYPE_USER

    @property
    def media_item_types(self) -> tuple[int, ...]:
        """The media types present, deduplicated, in the order received."""
        return tuple(dict.fromkeys(item.type for item in self.item_list if item.is_media))

    @property
    def has_unhandled_media(self) -> bool:
        """True when the message carried media this client cannot turn into text.

        A forwarded screenshot arrives as an image item with nothing beside it,
        so without this an image-only message is indistinguishable from an
        empty one -- and Sonder routes those pictures to its own ingest path
        rather than treating them as a message with nothing in it.
        """
        return any(item.is_unhandled_media for item in self.item_list)

    @property
    def unknown_item_types(self) -> tuple[int, ...]:
        """Item types outside the set this module models, deduplicated.

        Reported rather than ignored: it is how a caller finds out the protocol
        grew a new kind of content before it quietly stops working.
        """
        return tuple(
            dict.fromkeys(item.type for item in self.item_list if not item.is_known)
        )


def extract_text(message: WeixinMessage) -> str:
    """The message's plain text, with everything else left out.

    Every item that carries words contributes them: a caption and its image can
    arrive as two items of one message, and the server's transcript of a voice
    note is the note. Items of unknown type contribute nothing, which is the
    tolerant reading -- a type this client has not seen must not be able to
    take the text that came with it down with it.
    """
    return "\n".join(item.text for item in message.item_list if item.text)


@dataclass(frozen=True, slots=True)
class UpdatesPage:
    """One long-poll result from `getupdates`.

    `cursor` is what the next call must send back: the server replays
    everything a client has not acknowledged, so dropping the new cursor
    duplicates every message that was just handled. `timed_out` says the server
    held the connection past our read timeout, which is an ordinary empty poll.
    """

    messages: tuple[WeixinMessage, ...] = ()
    cursor: str = ""
    longpolling_timeout_ms: int | None = None
    timed_out: bool = False


@dataclass(frozen=True, slots=True)
class LoginQrcode:
    """A freshly created pairing code.

    `qrcode_img_content` is a URL despite the name: the official client renders
    it as a QR image locally and prints it as a fallback link, and either way
    what consumes it is the user's WeChat app.
    """

    qrcode: str
    qrcode_img_content: str


@dataclass(frozen=True, slots=True)
class LoginStatus:
    """One poll of the pairing state.

    `status` stays the raw wire string ("wait", "scaned", "confirmed",
    "expired", "need_verifycode", "verify_code_blocked", "binded_redirect",
    "scaned_but_redirect"). It is deliberately not an enum: the instruction the
    protocol implies is "keep polling until confirmed", so a status this client
    has not heard of has to read as "not yet" rather than as a parse failure.
    """

    status: str
    bot_token: str | None = None
    base_url: str | None = None
    bot_id: str | None = None
    user_id: str | None = None
    redirect_host: str | None = None

    @property
    def is_confirmed(self) -> bool:
        """True once the user scanned and the gateway issued a token."""
        return self.status == LOGIN_CONFIRMED


@dataclass(frozen=True, slots=True)
class LoginResult:
    """A completed pairing: the credential, and where to spend it.

    Both fields have to be persisted together. The token only authenticates
    against the host that issued it, and the gateway may name a different IDC
    than the default, so a token kept without its base URL authenticates
    nowhere.
    """

    bot_token: str
    base_url: str
    bot_id: str | None = None
    user_id: str | None = None


@dataclass(frozen=True, slots=True)
class BotConfig:
    """Per-conversation bot config. `typing_ticket` is all of it that matters today."""

    typing_ticket: str = ""


@dataclass(frozen=True, slots=True)
class UploadRequest:
    """Everything the gateway needs to pre-sign a CDN upload.

    Field names follow the protocol's `GetUploadUrlReq`. `rawsize` and
    `rawfilemd5` describe the plaintext file; `filesize` is the size *after*
    AES-128-ECB encryption, which the official client does before uploading, so
    the two sizes differ by the padding.
    """

    filekey: str
    media_type: int
    to_user_id: str
    rawsize: int
    rawfilemd5: str
    filesize: int
    aeskey: str
    thumb_rawsize: int | None = None
    thumb_rawfilemd5: str | None = None
    thumb_filesize: int | None = None
    no_need_thumb: bool | None = None

    def to_wire(self) -> dict[str, Any]:
        """The request body, without the keys the caller left unset."""
        payload: dict[str, Any] = {
            "filekey": self.filekey,
            "media_type": self.media_type,
            "to_user_id": self.to_user_id,
            "rawsize": self.rawsize,
            "rawfilemd5": self.rawfilemd5,
            "filesize": self.filesize,
            "aeskey": self.aeskey,
        }
        if self.thumb_rawsize is not None:
            payload["thumb_rawsize"] = self.thumb_rawsize
        if self.thumb_rawfilemd5 is not None:
            payload["thumb_rawfilemd5"] = self.thumb_rawfilemd5
        if self.thumb_filesize is not None:
            payload["thumb_filesize"] = self.thumb_filesize
        if self.no_need_thumb is not None:
            payload["no_need_thumb"] = self.no_need_thumb
        return payload


@dataclass(frozen=True, slots=True)
class UploadTarget:
    """A pre-signed upload destination.

    `upload_full_url` is the whole URL when the gateway supplies one; otherwise
    the caller builds it from `upload_param` and the CDN host.
    """

    upload_param: str | None = None
    thumb_upload_param: str | None = None
    upload_full_url: str | None = None


class ILinkClient:
    """A paired bot session.

    One instance per token, and one connection pool per instance: the pool is
    the reason a long-polling loop can run for hours without a handshake per
    minute, so it is created once here and closed by `aclose`.
    """

    def __init__(
        self,
        bot_token: str,
        base_url: str | None = None,
        *,
        base_info: BaseInfo | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        token = bot_token.strip()
        if not token:
            raise ValueError(
                "ILinkClient needs the bot token from wait_for_login(); an "
                "unauthenticated client cannot call any endpoint but the two "
                "pairing ones, which are module-level functions."
            )
        self._bot_token = token
        self._base_url = _normalize_base_url(base_url or DEFAULT_BASE_URL)
        self._base_info = base_info or BaseInfo()
        self._longpoll_timeout_s = LONGPOLL_TIMEOUT_S
        # Redirects are not followed because a 3xx would replay the bearer
        # token at whatever host the response named.
        self._http = http_client or httpx.AsyncClient(
            timeout=DEFAULT_TIMEOUT_S, follow_redirects=False
        )

    @property
    def base_url(self) -> str:
        """The host this client talks to. Safe to log; the token is not part of it."""
        return self._base_url

    async def aclose(self) -> None:
        """Close the connection pool.

        A client passed to the constructor is closed too: the session belongs to
        this object for its lifetime, so there is one owner to reason about
        instead of a flag deciding who closes what.
        """
        await self._http.aclose()

    def _headers(self) -> dict[str, str]:
        """Headers for one authenticated request.

        `X-WECHAT-UIN` is the anti-replay nonce: a fresh random uint32 rendered
        as a decimal string and base64-encoded, regenerated for every request.
        Caching it would make two requests share a value and defeat the only
        thing it does, so the headers cannot live on the shared client.
        """
        return {
            "Content-Type": "application/json",
            "AuthorizationType": AUTHORIZATION_TYPE,
            "X-WECHAT-UIN": _random_wechat_uin(),
            "Authorization": f"Bearer {self._bot_token}",
        }

    async def _post(
        self, endpoint: str, body: Mapping[str, Any], *, timeout: float
    ) -> dict[str, Any]:
        """POST one authenticated call and return its decoded body.

        `base_info` is added here rather than at each call site so no endpoint
        can forget it. Nothing is retried: a retried `sendmessage` can be
        delivered twice, and only the caller knows whether that is acceptable.
        """
        payload = {**body, "base_info": self._base_info.to_wire()}
        return await _perform(
            self._http,
            "POST",
            f"{self._base_url}/{endpoint}",
            endpoint=endpoint,
            timeout=timeout,
            headers=self._headers(),
            json_body=payload,
        )

    async def get_updates(self, cursor: str) -> UpdatesPage:
        """Long-poll for messages that arrived since `cursor`.

        The server holds the request for up to 35 s, so a read timeout here is
        how an empty answer normally arrives and it comes back as an empty page
        carrying the same cursor. The cursor is what makes that safe: the server
        replays from it, so it is carried forward untouched, and the
        `longpolling_timeout_ms` the response suggests is adopted for the next
        call so our read timeout tracks the server's own.
        """
        try:
            payload = await self._post(
                _ENDPOINT_GET_UPDATES,
                {"get_updates_buf": cursor},
                timeout=self._longpoll_timeout_s,
            )
        except ILinkTransportError as exc:
            # Only a read timeout is an ordinary empty poll. A refused
            # connection or a DNS failure is the caller's loop to handle, and
            # swallowing it here would turn a dead endpoint into a spin.
            if not exc.timed_out:
                raise
            log.debug("wechat_getupdates_timed_out", base_url=self._base_url)
            return UpdatesPage(cursor=cursor, timed_out=True)

        _raise_for_status(_ENDPOINT_GET_UPDATES, payload)
        suggested = _as_int(payload.get("longpolling_timeout_ms"))
        if suggested is not None and suggested > 0:
            self._longpoll_timeout_s = suggested / 1000
        return UpdatesPage(
            messages=tuple(
                _parse_message(entry) for entry in _as_mappings(payload.get("msgs"))
            ),
            # An empty cursor means "unchanged", not "reset": the official
            # client keeps its previous buffer in that case, and a caller that
            # reset here would replay the whole backlog.
            cursor=_as_str(payload.get("get_updates_buf")) or cursor,
            longpolling_timeout_ms=suggested,
        )

    async def send_text(self, to_user_id: str, text: str, context_token: str) -> None:
        """Send one text message, or raise.

        `context_token` is echoed verbatim from the inbound message, because it
        decides which conversation window the text lands in and the user id does
        not. An empty one is logged rather than refused, which is what the
        official client does: the gateway takes it, and refusing here would
        strand a reply the user is watching for over a bug the caller can see in
        the log. Empty text *is* refused -- the gateway accepts that too, and
        shows the user an empty bubble, which is a delivery failure that looks
        like a success.
        """
        if not text.strip():
            raise ValueError(
                "send_text refuses empty text: the gateway would accept it and "
                "show the user an empty bubble, which is a delivery failure "
                "that looks like a success."
            )
        if not context_token:
            log.warning("wechat_context_token_missing", to_user_id=to_user_id)
        message = {
            "to_user_id": to_user_id,
            "message_type": MESSAGE_TYPE_BOT,
            "message_state": MESSAGE_STATE_FINISH,
            "context_token": context_token,
            "item_list": [{"type": ITEM_TEXT, "text_item": {"text": text}}],
        }
        payload = await self._post(
            _ENDPOINT_SEND_MESSAGE, {"msg": message}, timeout=SEND_TIMEOUT_S
        )
        _raise_for_status(_ENDPOINT_SEND_MESSAGE, payload)
        log.debug("wechat_text_sent", to_user_id=to_user_id, chars=len(text))

    async def reply(self, message: WeixinMessage, text: str) -> None:
        """Reply into the conversation an inbound message came from.

        Exists so the two halves of a reply cannot be paired with the wrong
        conversation: the token is not derivable from the user id, and a
        mismatched one lands in another window without the gateway complaining.
        """
        await self.send_text(message.from_user_id, text, message.context_token)

    async def get_config(self, ilink_user_id: str, context_token: str) -> BotConfig:
        """Fetch bot config for a conversation, which means its typing ticket.

        The ticket is long-lived -- the official client caches it per user for a
        day, with jitter -- so a caller should cache this result rather than
        fetch it per message.
        """
        payload = await self._post(
            _ENDPOINT_GET_CONFIG,
            {"ilink_user_id": ilink_user_id, "context_token": context_token},
            timeout=CONFIG_TIMEOUT_S,
        )
        _raise_for_status(_ENDPOINT_GET_CONFIG, payload)
        return BotConfig(typing_ticket=_as_str(payload.get("typing_ticket")))

    async def send_typing(
        self, ilink_user_id: str, typing_ticket: str, *, active: bool = True
    ) -> None:
        """Show (`active=True`) or retract the typing bubble.

        WeChat retracts it on its own after a few seconds, which is why the
        official client re-sends it every 5 s while it is still generating.

        A conversation without a ticket has no bubble to show and the request is
        skipped: the gateway would reject it, and the caller can do nothing
        about that.
        """
        if not typing_ticket:
            log.debug("wechat_typing_skipped", ilink_user_id=ilink_user_id)
            return
        payload = await self._post(
            _ENDPOINT_SEND_TYPING,
            {
                "ilink_user_id": ilink_user_id,
                "typing_ticket": typing_ticket,
                "status": TYPING_STATUS_TYPING if active else TYPING_STATUS_CANCEL,
            },
            timeout=CONFIG_TIMEOUT_S,
        )
        _raise_for_status(_ENDPOINT_SEND_TYPING, payload)

    async def get_upload_url(self, request: UploadRequest) -> UploadTarget:
        """Pre-sign a CDN upload for a file that is about to be sent."""
        payload = await self._post(
            _ENDPOINT_GET_UPLOAD_URL, request.to_wire(), timeout=SEND_TIMEOUT_S
        )
        _raise_for_status(_ENDPOINT_GET_UPLOAD_URL, payload)
        return UploadTarget(
            upload_param=_as_str_or_none(payload.get("upload_param")),
            thumb_upload_param=_as_str_or_none(payload.get("thumb_upload_param")),
            upload_full_url=_as_str_or_none(payload.get("upload_full_url")),
        )

    async def notify_start(self) -> None:
        """Announce that a listener is starting.

        Sent before the first `getupdates`, so the gateway can tell "this bot is
        running and there is nothing new" apart from "this bot is gone".
        """
        payload = await self._post(_ENDPOINT_NOTIFY_START, {}, timeout=CONFIG_TIMEOUT_S)
        _raise_for_status(_ENDPOINT_NOTIFY_START, payload)

    async def notify_stop(self) -> None:
        """Announce that the listener is stopping, on the way out.

        Worth its own call rather than being implied by a dead connection: the
        pairing stays valid, and the gateway uses this to stop holding messages
        for a listener that is no longer there.
        """
        payload = await self._post(_ENDPOINT_NOTIFY_STOP, {}, timeout=CONFIG_TIMEOUT_S)
        _raise_for_status(_ENDPOINT_NOTIFY_STOP, payload)


async def request_login_qrcode(
    *,
    bot_type: str = DEFAULT_BOT_TYPE,
    base_url: str = DEFAULT_BASE_URL,
    http_client: httpx.AsyncClient | None = None,
    timeout: float = CONFIG_TIMEOUT_S,
) -> LoginQrcode:
    """Ask for a pairing code and hand it back for the user to scan.

    Unauthenticated on purpose: this is the call that produces the token every
    other call needs, which is why it is a module-level function rather than a
    method that would have to exist on a client that cannot be constructed yet.

    The official client sends this as a POST carrying the tokens it already
    holds (`local_token_list`); the endpoint also answers an unauthenticated
    GET, which is what is used here.
    """
    endpoint = _ENDPOINT_QRCODE
    async with _borrowed(http_client) as http:
        payload = await _perform(
            http,
            "GET",
            f"{_normalize_base_url(base_url)}/{endpoint}",
            endpoint=endpoint,
            timeout=timeout,
            params={"bot_type": bot_type},
        )
    _raise_for_status(endpoint, payload)
    qrcode = _as_str(payload.get("qrcode"))
    if not qrcode:
        raise ILinkProtocolError(
            f"{endpoint} returned no qrcode id, so there is nothing to poll: {payload!r}"
        )
    return LoginQrcode(
        qrcode=qrcode,
        qrcode_img_content=_as_str(payload.get("qrcode_img_content")),
    )


async def poll_login_status(
    qrcode: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
    http_client: httpx.AsyncClient | None = None,
    timeout: float = QR_POLL_TIMEOUT_S,
) -> LoginStatus:
    """Check the pairing state once.

    The endpoint long-polls as well, holding the request until something
    changes, so the timeout is generous on purpose and a timeout means "nothing
    has happened yet" rather than a failure. Transport and gateway errors are
    read the same way: the loop above is what retries, and a pairing should not
    die because one poll crossed a CDN hiccup.
    """
    endpoint = _ENDPOINT_QRCODE_STATUS
    async with _borrowed(http_client) as http:
        try:
            payload = await _perform(
                http,
                "GET",
                f"{_normalize_base_url(base_url)}/{endpoint}",
                endpoint=endpoint,
                timeout=timeout,
                params={"qrcode": qrcode},
            )
        except ILinkTransportError as exc:
            log.warning("wechat_login_poll_unreachable", reason=str(exc))
            return LoginStatus(status=LOGIN_WAIT)
        except ILinkAPIError as exc:
            log.warning(
                "wechat_login_poll_refused",
                status_code=exc.status_code,
                reason=str(exc),
            )
            return LoginStatus(status=LOGIN_WAIT)

    status = _as_str(payload.get("status"))
    if not status:
        # No status is not "keep waiting": either the body is reporting a
        # failure, which is worth raising, or it is not an answer about this
        # code at all, and polling it for eight minutes would hide that.
        _raise_for_status(endpoint, payload)
        raise ILinkProtocolError(f"{endpoint} returned no status: {payload!r}")
    return LoginStatus(
        status=status,
        bot_token=_as_str_or_none(payload.get("bot_token")),
        base_url=_as_str_or_none(payload.get("baseurl")),
        bot_id=_as_str_or_none(payload.get("ilink_bot_id")),
        user_id=_as_str_or_none(payload.get("ilink_user_id")),
        redirect_host=_as_str_or_none(payload.get("redirect_host")),
    )


async def wait_for_login(
    qrcode: str,
    *,
    timeout: float = LOGIN_TIMEOUT_S,
    interval: float = POLL_INTERVAL_S,
    base_url: str = DEFAULT_BASE_URL,
    http_client: httpx.AsyncClient | None = None,
) -> LoginResult:
    """Poll until the user scans the code, or give up.

    Every status other than "confirmed" means "keep waiting": the documented
    set (wait, scaned, need_verifycode, expired, ...) describes the states a
    pairing passes through, and one this client has not heard of should not end
    a pairing that is about to succeed.

    `scaned_but_redirect` is the exception worth acting on -- it names the IDC
    the account actually belongs to, and continuing to poll the original host is
    the difference between a login that completes and one that times out.
    """
    deadline = time.monotonic() + timeout
    host = _normalize_base_url(base_url)
    # One client for every poll: a pairing runs for minutes, and a fresh pool
    # per second would pay for a TLS handshake each time.
    async with _borrowed(http_client) as http:
        while True:
            status = await poll_login_status(qrcode, base_url=host, http_client=http)
            if status.is_confirmed:
                return _login_result(status, host)
            if status.redirect_host:
                host = _normalize_base_url(status.redirect_host)
                log.info("wechat_login_redirect", host=host)
            if time.monotonic() >= deadline:
                raise ILinkLoginTimeout(
                    f"the pairing code was not confirmed within {timeout:g}s. "
                    "Codes expire, so ask for a new one and scan it while it is "
                    "on screen."
                )
            await asyncio.sleep(interval)


def _login_result(status: LoginStatus, fallback_base_url: str) -> LoginResult:
    """Turn a confirmed status into the credential the caller has to persist.

    The token only authenticates against the host that issued it, so the
    response's own `baseurl` wins and the host that was polled is only the
    fallback for a response that omits it.
    """
    if not status.bot_token:
        raise ILinkProtocolError(
            "the pairing was reported confirmed without a bot_token, so there is "
            "nothing to persist and the code has been consumed. Start a new "
            "pairing."
        )
    return LoginResult(
        bot_token=status.bot_token,
        base_url=_normalize_base_url(status.base_url or fallback_base_url),
        bot_id=status.bot_id,
        user_id=status.user_id,
    )


@asynccontextmanager
async def _borrowed(
    http_client: httpx.AsyncClient | None,
) -> AsyncIterator[httpx.AsyncClient]:
    """Use the caller's client, or make one for this call and close it after.

    The pairing loop passes one in so its polls share a connection; a one-shot
    helper can afford a fresh client, and closing it here means a caller who
    does not care about pooling never leaks one.
    """
    if http_client is not None:
        yield http_client
        return
    async with httpx.AsyncClient(
        timeout=DEFAULT_TIMEOUT_S, follow_redirects=False
    ) as made:
        yield made


async def _perform(
    http: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    endpoint: str,
    timeout: float,
    headers: Mapping[str, str] | None = None,
    json_body: Mapping[str, Any] | None = None,
    params: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Make one call and decode it, naming the endpoint in every failure.

    The endpoint name is in the message because a transport error from httpx on
    its own says nothing about which call it came from, and in a long-polling
    loop that is the first thing anyone needs to know.
    """
    try:
        response = await http.request(
            method,
            url,
            headers=headers,
            json=json_body,
            params=params,
            timeout=timeout,
        )
    except httpx.TimeoutException as exc:
        raise ILinkTransportError(
            f"{endpoint} timed out after {timeout:g}s", endpoint=endpoint, timed_out=True
        ) from exc
    except httpx.HTTPError as exc:
        raise ILinkTransportError(
            f"{endpoint} failed: {exc!r}", endpoint=endpoint
        ) from exc
    return _decode(response, endpoint)


def _decode(response: httpx.Response, endpoint: str) -> dict[str, Any]:
    """Decode a JSON object, or explain what came back instead.

    The gateway sits behind a CDN that answers with HTML error pages -- a 524 is
    the documented one -- so a non-JSON body is a real outcome rather than a
    theoretical one, and a bare decode error would report it without any of
    that context.
    """
    if response.status_code >= 400:
        raise ILinkAPIError(
            f"{endpoint} returned HTTP {response.status_code}: {response.text[:200]!r}",
            endpoint=endpoint,
            status_code=response.status_code,
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise ILinkProtocolError(
            f"{endpoint} returned a non-JSON body (HTTP {response.status_code}): "
            f"{response.text[:200]!r}"
        ) from exc
    if not isinstance(payload, dict):
        raise ILinkProtocolError(
            f"{endpoint} returned {type(payload).__name__}, expected an object"
        )
    return payload


def _raise_for_status(endpoint: str, payload: Mapping[str, Any]) -> None:
    """Raise if the body reports a failure.

    Every endpoint is checked, including the ones whose answer a caller only
    uses for its side effect. This API replies HTTP 200 with `ret` set, so an
    unchecked response is indistinguishable from a delivered message -- and a
    reply that silently never arrived is the worst thing this module could do.
    """
    ret = _as_int(payload.get("ret"))
    errcode = _as_int(payload.get("errcode"))
    # A stale token arrives in whichever field the endpoint happens to use, so
    # the first non-zero code decides.
    code = next((value for value in (errcode, ret) if value), None)
    if code is None:
        return
    errmsg = _as_str(payload.get("errmsg"))
    detail = f"{endpoint} refused the request: ret={ret} errcode={errcode}"
    if errmsg:
        detail = f"{detail} errmsg={errmsg!r}"
    if code == STALE_TOKEN_ERRCODE:
        raise ILinkStaleToken(
            f"{detail}. The bot token is no longer valid, so the pairing has to "
            "be redone; retrying will not help.",
            endpoint=endpoint,
            ret=ret,
            errcode=errcode,
            errmsg=errmsg,
        )
    raise ILinkAPIError(
        detail, endpoint=endpoint, ret=ret, errcode=errcode, errmsg=errmsg
    )


def _parse_message(raw: Mapping[str, Any]) -> WeixinMessage:
    """Turn one entry of `msgs` into a message.

    Nothing in here raises on a missing or unfamiliar field: a message the
    caller can inspect and decide about is worth more than an exception that
    loses the batch it arrived in.
    """
    return WeixinMessage(
        from_user_id=_as_str(raw.get("from_user_id")),
        to_user_id=_as_str(raw.get("to_user_id")),
        message_type=_as_int(raw.get("message_type")) or MESSAGE_TYPE_NONE,
        context_token=_as_str(raw.get("context_token")),
        item_list=tuple(_parse_item(item) for item in _as_mappings(raw.get("item_list"))),
        message_id=_as_int(raw.get("message_id")),
        seq=_as_int(raw.get("seq")),
        session_id=_as_str_or_none(raw.get("session_id")),
        client_id=_as_str_or_none(raw.get("client_id")),
        run_id=_as_str_or_none(raw.get("run_id")),
        create_time_ms=_as_int(raw.get("create_time_ms")),
        # Copied, so a frozen message cannot be mutated through the payload it
        # was parsed from.
        raw=dict(raw),
    )


def _parse_item(raw: Mapping[str, Any]) -> MessageItem:
    """Turn one entry of `item_list` into an item.

    Text is read from the sub-object the item's type points at and from nowhere
    else. A future item type must not be able to contribute words to a
    conversation just because it happens to carry a `text` key.
    """
    item_type = _as_int(raw.get("type")) or ITEM_NONE
    text: str | None = None
    if item_type == ITEM_TEXT:
        text_item = raw.get("text_item")
        if isinstance(text_item, Mapping):
            text = _as_str_or_none(text_item.get("text"))
    elif item_type == ITEM_VOICE:
        # The server transcribes voice notes itself. When it did, the transcript
        # is the message, and nothing needs to download the silk audio to answer
        # it -- an empty transcript is normalised to None so it reads as
        # "untranscribed" rather than as a message with no words.
        voice_item = raw.get("voice_item")
        if isinstance(voice_item, Mapping):
            text = _as_str_or_none(voice_item.get("text"))
    return MessageItem(type=item_type, text=text)


def _random_wechat_uin() -> str:
    """The `X-WECHAT-UIN` value: a random uint32, decimal, base64-encoded.

    Regenerated per request by design, because it is the anti-replay nonce and a
    cached value would be the one thing it must not be.
    """
    return base64.b64encode(str(secrets.randbits(32)).encode()).decode()


def _normalize_base_url(url: str) -> str:
    """A base URL an endpoint path can be joined onto.

    Responses disagree about the trailing slash and a redirect host arrives as a
    bare hostname, so both are folded into one shape here instead of at each of
    the nine call sites.
    """
    cleaned = url.strip().rstrip("/")
    if not cleaned:
        return DEFAULT_BASE_URL
    if not cleaned.startswith(("http://", "https://")):
        cleaned = f"https://{cleaned}"
    return cleaned


def _as_str(value: Any, default: str = "") -> str:
    """Coerce a wire value to a string.

    This API is a JSON view of a protobuf schema, and proto3 JSON allows a
    64-bit id to arrive as either a string or a number, so id-shaped fields are
    read through here rather than trusted to be one type.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return str(value)
    return default


def _as_str_or_none(value: Any) -> str | None:
    """Like `_as_str`, but an absent or empty value stays absent.

    The protocol spells "not there" as a missing key, as null and as an empty
    string depending on the field, and collapsing all three onto None is what
    lets a caller test once.
    """
    text = _as_str(value)
    return text or None


def _as_int(value: Any, default: int | None = None) -> int | None:
    """Coerce a wire value to an int, or return `default`."""
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return default
    return default


def _as_mappings(value: Any) -> list[Mapping[str, Any]]:
    """Every object in a wire list, skipping anything that is not one."""
    if not isinstance(value, list):
        return []
    return [entry for entry in value if isinstance(entry, Mapping)]
