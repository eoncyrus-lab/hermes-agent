"""Small operational CLI for credentials and local-provider checks."""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import httpx
from pydantic import ValidationError
from sqlalchemy import select, update

from .api.auth import mint_token
from .config import settings
from .storage.db import maintenance_session
from .storage.models import ApiTokenRow, User

EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_SERVICE = 3
EXIT_NOT_FOUND = 4


async def _user_exists(user_id: UUID) -> bool:
    async with maintenance_session() as session:
        rows = (await session.execute(select(User.id).where(User.id == user_id))).all()
    return bool(rows)


async def _user_create(with_token: bool, label: str | None) -> int:
    """Create an account.

    This is the bootstrap: a fresh deployment has no users, and every other
    command needs one. Without it the only way in was hand-written SQL against
    the container, which is a bad first instruction to give an operator.
    """
    async with maintenance_session() as session:
        user = User()
        session.add(user)
        await session.flush()
        user_id = user.id
        plaintext = None
        if with_token:
            plaintext, digest = mint_token()
            session.add(ApiTokenRow(user_id=user_id, token_sha256=digest, label=label))
    print(user_id)
    if plaintext is not None:
        # Shown once and never stored -- only the digest is kept.
        print(plaintext)
    return EXIT_OK


async def _user_list() -> int:
    async with maintenance_session() as session:
        rows = (
            (await session.execute(select(User).order_by(User.created_at, User.id)))
            .scalars()
            .all()
        )
    if not rows:
        print("No users.")
        return EXIT_OK
    for row in rows:
        print(f"{row.id}\tcreated={row.created_at.isoformat()}")
    return EXIT_OK


async def _relationship_create(user_id: UUID, name: str, retention_days: int) -> int:
    """Create a third-party profile and a relationship pointing at it.

    The last of the bootstrap gaps: there was no route and no command for
    this, only DELETE. A fresh deployment could link a bot and then had
    nothing to talk about.

    `retention_until` is required by the schema for a third-party subject --
    modelling someone who is not your user is only acceptable with an expiry
    on it, so there is no way to create one without setting it.
    """
    from datetime import timedelta

    from .storage.models import PersonProfileRow, RelationshipRow

    if not await _user_exists(user_id):
        print(f"No such user: {user_id}")
        return EXIT_NOT_FOUND
    if retention_days < 1:
        print("Retention must be at least one day.")
        return EXIT_CONFIG

    async with maintenance_session() as session:
        person = PersonProfileRow(
            user_id=user_id,
            subject="target",
            data_subject="third_party",
            display_name=name,
            version=1,
            retention_until=datetime.now(UTC) + timedelta(days=retention_days),
        )
        session.add(person)
        await session.flush()
        relationship = RelationshipRow(user_id=user_id, target_person_id=person.id)
        session.add(relationship)
        await session.flush()
        relationship_id = relationship.id
    print(relationship_id)
    return EXIT_OK


async def _relationship_list(user_id: UUID) -> int:
    from .storage.models import PersonProfileRow, RelationshipRow

    async with maintenance_session() as session:
        rows = (
            await session.execute(
                select(
                    RelationshipRow.id,
                    PersonProfileRow.display_name,
                    PersonProfileRow.retention_until,
                )
                .join(
                    PersonProfileRow,
                    PersonProfileRow.id == RelationshipRow.target_person_id,
                )
                .where(RelationshipRow.user_id == user_id)
                .order_by(PersonProfileRow.display_name, RelationshipRow.id)
            )
        ).all()
    if not rows:
        print("No relationships.")
        return EXIT_OK
    for rid, name, until in rows:
        expiry = until.isoformat() if until else "-"
        print(f"{rid}\t{name}\tretention_until={expiry}")
    return EXIT_OK


async def _telegram_link(user_id: UUID, telegram_id: int) -> int:
    """Bind a Telegram account to a Sonder account.

    Without this the bot can only say "this chat is not linked" -- the mapping
    is what `auth_telegram` resolves, and there was no way to create one short
    of hand-written SQL.
    """
    from .storage.models import TelegramAccountRow

    if not await _user_exists(user_id):
        print(f"No such user: {user_id}")
        return EXIT_NOT_FOUND
    async with maintenance_session() as session:
        existing = await session.get(TelegramAccountRow, telegram_id)
        if existing is not None:
            if existing.user_id == user_id:
                print(f"Already linked to {user_id}.")
                return EXIT_OK
            print(
                f"Telegram id {telegram_id} is already linked to another account "
                f"({existing.user_id}). Unlink it first."
            )
            return EXIT_NOT_FOUND
        session.add(TelegramAccountRow(telegram_user_id=telegram_id, user_id=user_id))
    print(f"Linked Telegram {telegram_id} to {user_id}.")
    return EXIT_OK


async def _credentials_set(
    user_id: UUID,
    provider: str,
    profile: str,
    base_url: str | None,
    model: str | None,
    intimate: bool,
) -> int:
    """Store a model credential for one account.

    The key is read from the terminal rather than taken as an argument: argv
    is in the shell history and in `ps` for every user on the box.
    """
    import getpass

    from .llm import user_credentials
    from .llm.configuration import PROFILES

    if not await _user_exists(user_id):
        print(f"No such user: {user_id}")
        return EXIT_NOT_FOUND
    if profile not in PROFILES:
        print(f"Unknown profile '{profile}'. Known: {', '.join(sorted(PROFILES))}")
        return EXIT_CONFIG

    api_key = getpass.getpass(f"API key for {provider} (not echoed): ").strip()
    if not api_key:
        print("Empty key; nothing written.")
        return EXIT_CONFIG

    async with maintenance_session() as session:
        await user_credentials.save(
            session,
            user_id,
            provider=provider,
            profile=profile,
            api_key=api_key,
            base_url=base_url,
            model=model,
            purpose=user_credentials.INTIMATE if intimate else user_credentials.GENERAL,
        )
        refused = []
        if not intimate:
            from .config import settings

            refused = sorted(
                user_credentials.safety_provider_names(settings().llm_routes) & {provider}
            )
    print(f"Stored '{provider}' ({profile}) for {user_id}.")
    if refused:
        # Said plainly rather than discovered later: their key is real, and
        # the deployment's own chains keep using the operator's for this name.
        print(
            f"Note: '{provider}' serves EXTRACT or CRITIC in this deployment, so "
            "the operator's credential keeps that route. Yours is used for "
            "everything else."
        )
    return EXIT_OK


async def _credentials_list(user_id: UUID) -> int:
    from .llm import user_credentials

    if not await _user_exists(user_id):
        print(f"No such user: {user_id}")
        return EXIT_NOT_FOUND
    async with maintenance_session() as session:
        rows = await user_credentials.describe_for(session, user_id)
    if not rows:
        print("No credentials of their own; requests use the deployment's.")
        return EXIT_OK
    for row in rows:
        where = row["base_url"] or "(profile default)"
        model = row["model"] or "(route chain)"
        print(
            f"{row['provider']}\t{row['profile']}\t{row['purpose']}\t"
            f"{model}\t{where}\t{row['key_version']}"
        )
    return EXIT_OK


async def _credentials_clear(user_id: UUID, provider: str) -> int:
    from .llm import user_credentials

    async with maintenance_session() as session:
        removed = await user_credentials.remove(session, user_id, provider)
    if not removed:
        print(f"No credential named '{provider}' for {user_id}.")
        return EXIT_NOT_FOUND
    print(f"Removed '{provider}'.")
    return EXIT_OK


async def _wechat_link(user_id: UUID, wechat_id: str) -> int:
    """Bind a WeChat handle to a Sonder account.

    The handle is not something a person can read off their own screen, so the
    channel prints it back to them when it does not recognise them -- that
    message is the whole discovery mechanism, and this is the other half of
    it. Without this command the bot can only ever say "not linked".
    """
    from .storage.models import WechatAccountRow

    if not await _user_exists(user_id):
        print(f"No such user: {user_id}")
        return EXIT_NOT_FOUND
    async with maintenance_session() as session:
        existing = await session.get(WechatAccountRow, wechat_id)
        if existing is not None:
            if existing.user_id == user_id:
                print(f"Already linked to {user_id}.")
                return EXIT_OK
            print(
                f"WeChat {wechat_id} is already linked to another account "
                f"({existing.user_id}). Unlink it first."
            )
            return EXIT_NOT_FOUND
        session.add(WechatAccountRow(wechat_user_id=wechat_id, user_id=user_id))
    print(f"Linked WeChat {wechat_id} to {user_id}.")
    return EXIT_OK


async def _wechat_unlink(wechat_id: str) -> int:
    from .storage.models import WechatAccountRow

    async with maintenance_session() as session:
        existing = await session.get(WechatAccountRow, wechat_id)
        if existing is None:
            print(f"WeChat {wechat_id} is not linked.")
            return EXIT_NOT_FOUND
        await session.delete(existing)
    print(f"Unlinked {wechat_id}.")
    return EXIT_OK


async def _wechat_list() -> int:
    from .storage.models import WechatAccountRow

    async with maintenance_session() as session:
        rows = (
            (
                await session.execute(
                    select(WechatAccountRow).order_by(
                        WechatAccountRow.linked_at, WechatAccountRow.wechat_user_id
                    )
                )
            )
            .scalars()
            .all()
        )
    if not rows:
        print("No linked WeChat accounts.")
        return EXIT_OK
    for row in rows:
        print(f"{row.wechat_user_id}\t{row.user_id}\tlinked={row.linked_at.isoformat()}")
    return EXIT_OK


async def _telegram_unlink(telegram_id: int) -> int:
    from .storage.models import TelegramAccountRow

    async with maintenance_session() as session:
        existing = await session.get(TelegramAccountRow, telegram_id)
        if existing is None:
            print(f"Telegram id {telegram_id} is not linked.")
            return EXIT_NOT_FOUND
        await session.delete(existing)
    print(f"Unlinked {telegram_id}.")
    return EXIT_OK


async def _telegram_list() -> int:
    from .storage.models import TelegramAccountRow

    async with maintenance_session() as session:
        rows = (
            (
                await session.execute(
                    select(TelegramAccountRow).order_by(
                        TelegramAccountRow.linked_at, TelegramAccountRow.telegram_user_id
                    )
                )
            )
            .scalars()
            .all()
        )
    if not rows:
        print("No linked Telegram accounts.")
        return EXIT_OK
    for row in rows:
        print(
            f"{row.telegram_user_id}\t{row.user_id}\tlinked={row.linked_at.isoformat()}"
        )
    return EXIT_OK


async def _token_create(user_id: UUID, label: str | None) -> int:
    if not await _user_exists(user_id):
        print(f"No such user: {user_id}")
        return EXIT_NOT_FOUND
    plaintext, digest = mint_token()
    async with maintenance_session() as session:
        session.add(ApiTokenRow(user_id=user_id, token_sha256=digest, label=label))
    print(plaintext)
    return EXIT_OK


async def _token_list(user_id: UUID) -> int:
    if not await _user_exists(user_id):
        print(f"No such user: {user_id}")
        return EXIT_NOT_FOUND
    async with maintenance_session() as session:
        rows = (
            (
                await session.execute(
                    select(ApiTokenRow)
                    .where(ApiTokenRow.user_id == user_id)
                    .order_by(ApiTokenRow.created_at, ApiTokenRow.id)
                )
            )
            .scalars()
            .all()
        )
    if not rows:
        print("No tokens.")
        return EXIT_OK
    for row in rows:
        status = "revoked" if row.revoked_at else "active"
        label = row.label or "-"
        print(f"{row.id}\t{status}\t{label}\tcreated={row.created_at.isoformat()}")
    return EXIT_OK


async def _token_revoke(user_id: UUID, token_id: UUID) -> int:
    async with maintenance_session() as session:
        rows = (
            await session.execute(
                select(ApiTokenRow.id, ApiTokenRow.revoked_at).where(
                    ApiTokenRow.id == token_id,
                    ApiTokenRow.user_id == user_id,
                )
            )
        ).all()
        if not rows:
            print("No such token for that user.")
            return EXIT_NOT_FOUND
        if rows[0].revoked_at is None:
            await session.execute(
                update(ApiTokenRow)
                .where(
                    ApiTokenRow.id == token_id,
                    ApiTokenRow.user_id == user_id,
                    ApiTokenRow.revoked_at.is_(None),
                )
                .values(revoked_at=datetime.now(UTC))
            )
    print(f"Revoked {token_id}.")
    return EXIT_OK


async def _smoke_local() -> int:
    cfg = settings()
    base_url = cfg.local_base_url
    model = cfg.model_generate_intimate
    if not base_url:
        print("Local provider is not configured: set SONDER_LOCAL_BASE_URL.")
        return EXIT_CONFIG
    if not model:
        print(
            "Local provider model is not configured: set SONDER_MODEL_GENERATE_INTIMATE."
        )
        return EXIT_CONFIG

    headers = {"Authorization": f"Bearer {cfg.local_api_key}"}
    endpoint = base_url.rstrip("/")
    try:
        async with httpx.AsyncClient(
            headers=headers, timeout=cfg.request_timeout_s
        ) as client:
            models_response = await client.get(f"{endpoint}/models")
            models_response.raise_for_status()
            model_ids = {
                item.get("id")
                for item in models_response.json().get("data", [])
                if isinstance(item, dict)
            }
            if model not in model_ids:
                print(f"Local provider does not advertise configured model: {model}")
                return EXIT_SERVICE

            completion_response = await client.post(
                f"{endpoint}/chat/completions",
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": "Reply with OK."}],
                    "max_tokens": 8,
                    "temperature": 0,
                },
            )
            completion_response.raise_for_status()
            choices = completion_response.json().get("choices")
            if not isinstance(choices, list) or not choices:
                print("Local provider returned no completion choices.")
                return EXIT_SERVICE
    except (httpx.HTTPError, AttributeError, TypeError, ValueError):
        print(
            "Local provider smoke check failed while calling its OpenAI-compatible API."
        )
        return EXIT_SERVICE

    print(f"Local provider is reachable and completed a request with {model}.")
    return EXIT_OK


async def _llm_validate() -> int:
    """Resolve every task x track route, so a bad deployment fails here.

    The check that earns its keep is the safety guard: extraction and the
    Critic exist to notice discomfort and pressure, and a model tuned not to
    refuse is also tuned not to notice. That failure is silent in production
    and loud here.
    """
    from .llm.provider import ProviderError, Task, Track
    from .llm.router import Router, UnsafeRouting

    try:
        router = Router()
        router.validate_routes()
    except UnsafeRouting as exc:
        print(f"Unsafe route configuration: {exc}")
        return EXIT_CONFIG
    except ProviderError as exc:
        print(f"Invalid route configuration: {exc}")
        return EXIT_CONFIG

    providers = router.providers
    for name, reason in router.disabled_providers.items():
        print(f"provider\t{name}\tdisabled\t{reason}")
    if not providers:
        print("No provider is configured: set at least one provider API key.")
        return EXIT_CONFIG

    vouched = router.vouched
    for name, permissive in providers.items():
        if name in vouched:
            trust = "operator-trusted"
        elif permissive:
            trust = "permissive"
        else:
            trust = "safety-trusted"
        print(f"provider\t{name}\t{trust}")
    if vouched:
        print(
            "warning\tsafety-critical work is running on "
            + ",".join(sorted(vouched))
            + "\tvia SONDER_SAFETY_TRUSTED_PROVIDERS"
        )
    for task in Task:
        for track in Track:
            route = router.resolve(task, track)
            chain = router.chain(task, track)
            failover = ",".join(hop.provider_name for hop in chain[1:]) or "-"
            print(
                f"route\t{task.value}.{track.value}\t"
                f"{route.provider_name}\t{route.model}\tfailover={failover}"
            )
    return EXIT_OK


async def _llm_smoke(provider_name: str | None, model: str | None) -> int:
    """Send one real, tiny completion through a configured provider."""
    from .llm.provider import Msg, ProviderError
    from .llm.router import Router

    # The local slot keeps its own path: it also checks that the endpoint
    # advertises the model, which catches a stale SONDER_MODEL_* far more
    # clearly than a 404 from the completion call.
    if provider_name in (None, "local") and model is None:
        return await _smoke_local()
    if provider_name is None:
        provider_name = "local"
    if not model:
        print(f"No model given for '{provider_name}': pass --model.")
        return EXIT_CONFIG

    try:
        provider = Router().provider(provider_name)
    except ProviderError as exc:
        print(str(exc))
        return EXIT_CONFIG

    try:
        completion = await provider.complete(
            [Msg(role="user", content="Reply with OK.")],
            model=model,
            max_tokens=8,
            temperature=0,
        )
    except ProviderError as exc:
        print(f"Provider '{provider_name}' failed the smoke check: {exc}")
        return EXIT_SERVICE
    except (httpx.HTTPError, AttributeError, TypeError, ValueError) as exc:
        print(f"Provider '{provider_name}' failed the smoke check: {exc}")
        return EXIT_SERVICE

    print(
        f"Provider '{provider_name}' completed a request with "
        f"{completion.model or model}."
    )
    return EXIT_OK


async def _llm_smoke_route(task_name: str, track_name: str) -> int:
    """Exercise normal resolution and ordered failover for one route."""
    from .llm.provider import Msg, ProviderError, Task, Track
    from .llm.router import Router, UnsafeRouting

    task, track = Task(task_name), Track(track_name)
    try:
        router = Router()
        first = router.resolve(task, track)
        completion = await router.complete(
            task,
            [Msg(role="user", content="Reply with OK.")],
            track=track,
            max_tokens=8,
            temperature=0,
        )
    except (ProviderError, UnsafeRouting) as exc:
        print(f"Route '{task.value}.{track.value}' failed the smoke check: {exc}")
        return EXIT_SERVICE
    print(
        f"Route '{task.value}.{track.value}' started at '{first.provider_name}' "
        f"and completed with {completion.model}."
    )
    return EXIT_OK


def _uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a UUID") from exc


def _parser() -> argparse.ArgumentParser:
    from .llm.provider import Task, Track

    parser = argparse.ArgumentParser(prog="sonder")
    groups = parser.add_subparsers(dest="group", required=True)

    user = groups.add_parser("user", help="create and list accounts")
    user_commands = user.add_subparsers(dest="command", required=True)
    user_create = user_commands.add_parser(
        "create", help="create an account, optionally with its first token"
    )
    user_create.add_argument(
        "--with-token",
        action="store_true",
        help="also mint a bearer token and print it after the user id",
    )
    user_create.add_argument("--label", help="label for that token")
    user_commands.add_parser("list", help="list accounts")

    rel = groups.add_parser("relationship", help="create and list relationships")
    rel_commands = rel.add_subparsers(dest="command", required=True)
    rel_create = rel_commands.add_parser("create")
    rel_create.add_argument("--user", required=True, type=_uuid)
    rel_create.add_argument(
        "--name", required=True, help="display name, a pseudonym is fine"
    )
    rel_create.add_argument(
        "--retention-days",
        type=int,
        default=365,
        help="a third-party profile must expire; default 365",
    )
    rel_list = rel_commands.add_parser("list")
    rel_list.add_argument("--user", required=True, type=_uuid)

    telegram = groups.add_parser("telegram", help="link Telegram chats to accounts")
    telegram_commands = telegram.add_subparsers(dest="command", required=True)
    tg_link = telegram_commands.add_parser("link", help="bind a Telegram id")
    tg_link.add_argument("--user", required=True, type=_uuid)
    tg_link.add_argument("--telegram-id", required=True, type=int)
    tg_unlink = telegram_commands.add_parser("unlink")
    tg_unlink.add_argument("--telegram-id", required=True, type=int)
    telegram_commands.add_parser("list", help="list linked Telegram accounts")

    token = groups.add_parser("token", help="manage API bearer tokens")
    token_commands = token.add_subparsers(dest="command", required=True)
    create = token_commands.add_parser("create")
    create.add_argument("--user", required=True, type=_uuid)
    create.add_argument("--label")
    list_tokens = token_commands.add_parser("list")
    list_tokens.add_argument("--user", required=True, type=_uuid)
    revoke = token_commands.add_parser("revoke")
    revoke.add_argument("token_id", type=_uuid)
    revoke.add_argument("--user", required=True, type=_uuid)

    llm = groups.add_parser("llm", help="check model providers")
    llm_commands = llm.add_subparsers(dest="command", required=True)
    llm_commands.add_parser("validate", help="resolve and guard every route")
    smoke = llm_commands.add_parser("smoke", help="one real provider or route call")
    smoke_targets = smoke.add_subparsers(dest="target", required=True)
    smoke_provider = smoke_targets.add_parser("provider")
    smoke_provider.add_argument("name")
    smoke_provider.add_argument("--model", required=True)
    smoke_route = smoke_targets.add_parser("route")
    smoke_route.add_argument("task", choices=[task.value for task in Task])
    smoke_route.add_argument(
        "--track",
        choices=[track.value for track in Track],
        default=Track.STANDARD.value,
    )
    llm_commands.add_parser("smoke-local", help="smoke the legacy local route")

    creds = groups.add_parser("credentials", help="a user's own model keys")
    cred_commands = creds.add_subparsers(dest="command", required=True)
    cred_set = cred_commands.add_parser("set", help="store one (prompts for the key)")
    cred_set.add_argument("--user", required=True, type=_uuid)
    cred_set.add_argument("--provider", required=True, help="name a route chain uses")
    cred_set.add_argument("--profile", required=True, help="protocol/endpoint preset")
    cred_set.add_argument("--base-url", default=None)
    cred_set.add_argument("--model", default=None)
    cred_set.add_argument(
        "--intimate",
        action="store_true",
        help="send this user's intimate track here (a self-hosted model)",
    )
    cred_list = cred_commands.add_parser("list", help="what they have, never the key")
    cred_list.add_argument("--user", required=True, type=_uuid)
    cred_clear = cred_commands.add_parser("clear")
    cred_clear.add_argument("--user", required=True, type=_uuid)
    cred_clear.add_argument("--provider", required=True)

    wechat = groups.add_parser("wechat", help="pair the WeChat bot channel")
    wechat_commands = wechat.add_subparsers(dest="command", required=True)
    wechat_login = wechat_commands.add_parser(
        "login", help="show a QR code and store the token it returns"
    )
    wechat_login.add_argument(
        "--timeout",
        type=float,
        default=480.0,
        help="seconds to wait for the scan; default 480",
    )
    wechat_login.add_argument(
        "--env",
        default=None,
        help="path to the .env to update; default is the repository root",
    )
    wechat_login.add_argument(
        "--open",
        action="store_true",
        dest="open_image",
        help="open the QR image, when the terminal's own drawing will not scan",
    )
    wechat_commands.add_parser("status", help="report whether a bot is paired")
    wechat_link = wechat_commands.add_parser(
        "link", help="bind a WeChat handle to an account"
    )
    wechat_link.add_argument("--user", required=True, type=_uuid)
    wechat_link.add_argument(
        "--wechat-id",
        required=True,
        help="the handle the bot printed back, like o9cq...@im.wechat",
    )
    wechat_unlink = wechat_commands.add_parser("unlink")
    wechat_unlink.add_argument("--wechat-id", required=True)
    wechat_commands.add_parser("list", help="list linked WeChat accounts")
    return parser


async def _wechat_login(timeout: float, env: str | None, open_image: bool) -> int:
    """Pair a WeChat bot and store the token, or explain why it did not."""
    import webbrowser

    from .config import REPO_ROOT
    from .wechat.client import ILinkError, ILinkLoginTimeout
    from .wechat.login import BOT_TOKEN_KEY, run_login

    env_path = Path(env) if env else REPO_ROOT / ".env"
    # Written next to the cursor rather than into the repository root: it is a
    # live capability for the couple of minutes it lasts, and `storage/` is
    # already ignored.
    image_path = REPO_ROOT / "storage" / "wechat-login-qr.svg"

    try:
        result = await run_login(
            env_path,
            timeout=timeout,
            image_path=image_path,
            opener=(lambda p: webbrowser.open(p.resolve().as_uri()))
            if open_image
            else None,
        )
    except ILinkLoginTimeout:
        print("Timed out waiting for the scan. Run it again for a fresh code.")
        return EXIT_SERVICE
    except ILinkError as exc:
        print(f"Pairing failed: {exc}")
        return EXIT_SERVICE

    print(f"Paired. {BOT_TOKEN_KEY} written to {env_path}.")
    print(f"bot_id={result.bot_id} base_url={result.base_url}")
    print("Restart the WeChat channel to pick it up.")
    return EXIT_OK


async def _wechat_status() -> int:
    """Whether a bot is paired. Never prints the token, not even part of it."""
    token = settings().wechat_bot_token
    if not token:
        print("No WeChat bot paired. Run: sonder wechat login")
        return EXIT_CONFIG
    base = settings().wechat_base_url or "(default endpoint)"
    print(f"paired: token set ({len(token)} chars), base_url={base}")
    return EXIT_OK


def main() -> None:
    parser = _parser()
    args = parser.parse_args()
    try:
        code = _dispatch(parser, args)
    except ValidationError as exc:
        # A raw pydantic traceback is the least useful way to say "you are
        # missing an environment variable", and it is what an operator gets
        # for forgetting `SONDER_DATABASE_URL` -- the one thing every command
        # except `wechat login` needs and nothing prompts them for.
        missing = sorted(
            str(err["loc"][0]).upper()
            for err in exc.errors()
            if err.get("type") == "missing" and err.get("loc")
        )
        if missing:
            print(
                "Missing configuration: "
                + ", ".join(f"SONDER_{name}" for name in missing)
                + "\nSet it, or run through scripts/run-local.sh, which "
                "assembles the DSNs from .env."
            )
        else:
            print(f"Invalid configuration: {exc}")
        raise SystemExit(EXIT_CONFIG) from None
    raise SystemExit(code)


def _dispatch(parser, args) -> int:
    if args.group == "relationship" and args.command == "create":
        code = asyncio.run(
            _relationship_create(args.user, args.name, args.retention_days)
        )
    elif args.group == "relationship" and args.command == "list":
        code = asyncio.run(_relationship_list(args.user))
    elif args.group == "telegram" and args.command == "link":
        code = asyncio.run(_telegram_link(args.user, args.telegram_id))
    elif args.group == "telegram" and args.command == "unlink":
        code = asyncio.run(_telegram_unlink(args.telegram_id))
    elif args.group == "telegram" and args.command == "list":
        code = asyncio.run(_telegram_list())
    elif args.group == "user" and args.command == "create":
        code = asyncio.run(_user_create(args.with_token, args.label))
    elif args.group == "user" and args.command == "list":
        code = asyncio.run(_user_list())
    elif args.group == "token" and args.command == "create":
        code = asyncio.run(_token_create(args.user, args.label))
    elif args.group == "token" and args.command == "list":
        code = asyncio.run(_token_list(args.user))
    elif args.group == "token" and args.command == "revoke":
        code = asyncio.run(_token_revoke(args.user, args.token_id))
    elif args.group == "llm" and args.command == "validate":
        code = asyncio.run(_llm_validate())
    elif args.group == "llm" and args.command == "smoke" and args.target == "provider":
        code = asyncio.run(_llm_smoke(args.name, args.model))
    elif args.group == "llm" and args.command == "smoke" and args.target == "route":
        code = asyncio.run(_llm_smoke_route(args.task, args.track))
    elif args.group == "llm" and args.command == "smoke-local":
        code = asyncio.run(_llm_smoke(None, None))
    elif args.group == "credentials" and args.command == "set":
        code = asyncio.run(
            _credentials_set(
                args.user,
                args.provider,
                args.profile,
                args.base_url,
                args.model,
                args.intimate,
            )
        )
    elif args.group == "credentials" and args.command == "list":
        code = asyncio.run(_credentials_list(args.user))
    elif args.group == "credentials" and args.command == "clear":
        code = asyncio.run(_credentials_clear(args.user, args.provider))
    elif args.group == "wechat" and args.command == "login":
        code = asyncio.run(_wechat_login(args.timeout, args.env, args.open_image))
    elif args.group == "wechat" and args.command == "status":
        code = asyncio.run(_wechat_status())
    elif args.group == "wechat" and args.command == "link":
        code = asyncio.run(_wechat_link(args.user, args.wechat_id))
    elif args.group == "wechat" and args.command == "unlink":
        code = asyncio.run(_wechat_unlink(args.wechat_id))
    elif args.group == "wechat" and args.command == "list":
        code = asyncio.run(_wechat_list())
    else:
        # Reachable only when a subcommand is added to `_parser` and not here.
        # This used to fall through to a local-provider smoke test, so a new
        # command silently did the wrong thing instead of failing loudly.
        parser.error(f"unhandled command: {args.group} {args.command}")
    return code


if __name__ == "__main__":
    main()
