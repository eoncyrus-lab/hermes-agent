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
    if not providers:
        print("No provider is configured: set at least one provider API key.")
        return EXIT_CONFIG

    for name, permissive in providers.items():
        trust = "permissive" if permissive else "safety-trusted"
        print(f"provider\t{name}\t{trust}")
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


def _uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a UUID") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sonder")
    groups = parser.add_subparsers(dest="group", required=True)

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
    smoke = llm_commands.add_parser("smoke", help="one real call to a provider")
    smoke.add_argument("--provider")
    smoke.add_argument("--model")
    llm_commands.add_parser("smoke-local", help="alias for: smoke --provider local")
    return parser


def main() -> None:
    args = _parser().parse_args()
    if args.group == "token" and args.command == "create":
        code = asyncio.run(_token_create(args.user, args.label))
    elif args.group == "token" and args.command == "list":
        code = asyncio.run(_token_list(args.user))
    elif args.group == "token" and args.command == "revoke":
        code = asyncio.run(_token_revoke(args.user, args.token_id))
    elif args.group == "llm" and args.command == "validate":
        code = asyncio.run(_llm_validate())
    elif args.group == "llm" and args.command == "smoke":
        code = asyncio.run(_llm_smoke(args.provider, args.model))
    else:
        code = asyncio.run(_llm_smoke(None, None))
    raise SystemExit(code)


if __name__ == "__main__":
    main()
