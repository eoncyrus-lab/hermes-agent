"""Small operational CLI for credentials and local-provider checks."""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
from uuid import UUID

import httpx
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
    return parser


def main() -> None:
    args = _parser().parse_args()
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
    else:
        code = asyncio.run(_llm_smoke(None, None))
    raise SystemExit(code)


if __name__ == "__main__":
    main()
