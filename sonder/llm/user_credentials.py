"""Model credentials a user brings, and what they change about their routes.

Two things a user's own credential does, and one it must never do.

It **adds a provider** to that user's requests, overriding one of the
operator's if it carries the same name -- which is what "bring your own key
for the same vendor" means, and the only reading that makes a per-user key
useful.

It **redirects the intimate track** when it is marked `purpose='intimate'`.
That is the whole answer to a hosted model hedging on frank adult
conversation: point it at weights you host, rather than trying to talk a
vendor's model out of its refusals. `Track.INTIMATE` never reaches the safety
tasks in either direction, so this cannot move EXTRACT or CRITIC.

What it must never do is serve a safety-critical task. That is enforced one
layer down, in `router._register`: anything arriving with `user_supplied=True`
is permissive whatever its profile says, so a chain that names it for EXTRACT
is a startup error rather than a quiet route.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

import structlog
from sqlalchemy import delete, select

from ..storage import secrets
from ..storage.models import UserLlmCredentialRow
from .configuration import ProviderConfig, route_key
from .provider import Task, Track

log = structlog.get_logger(__name__)

GENERAL = "general"
INTIMATE = "intimate"


@dataclass(frozen=True, slots=True)
class UserCredential:
    """One decrypted row. The plaintext key lives no longer than the request."""

    provider: str
    profile: str
    base_url: str | None
    model: str | None
    purpose: str
    api_key: str


async def load_for(session, user_id: UUID) -> list[UserCredential]:
    """Every credential this user has, decrypted.

    A row whose key can no longer be decrypted is skipped with a warning
    rather than raising: one rotated-away credential should not make every
    request that user makes fail, and the operator can see which one it is.
    """
    rows = (
        (
            await session.execute(
                select(UserLlmCredentialRow).where(
                    UserLlmCredentialRow.user_id == user_id
                )
            )
        )
        .scalars()
        .all()
    )
    out: list[UserCredential] = []
    for row in rows:
        try:
            api_key = secrets.decrypt(row.ciphertext, row.key_version)
        except secrets.Undecryptable:
            log.warning("user_credential_undecryptable", provider=row.provider)
            continue
        out.append(
            UserCredential(
                provider=row.provider,
                profile=row.profile,
                base_url=row.base_url,
                model=row.model,
                purpose=row.purpose,
                api_key=api_key,
            )
        )
    return out


def as_provider_configs(creds: list[UserCredential]) -> dict[str, ProviderConfig]:
    """The registry entries these credentials contribute.

    An intimate-purpose credential is also registered as a provider, because
    the route that points at it still names it.
    """
    return {
        cred.provider: ProviderConfig(
            profile=cred.profile,
            api_key=cred.api_key,
            base_url=cred.base_url,
            user_supplied=True,
        )
        for cred in creds
    }


def routes_with_intimate(
    base: dict[str, list], creds: list[UserCredential]
) -> dict[str, list]:
    """Point the user's intimate track at their own endpoint.

    Only the three tasks that actually follow the track. Extraction and
    criticism deliberately do not, in either direction -- they exist to notice
    discomfort and pressure, and a model chosen for being willing is a model
    that will not notice. Redirecting `extract.intimate` here was the first
    thing this function did, and `router._guard` refused it at startup with
    `UnsafeRouting`, which is the guard doing exactly what it is for.

    Every track-following task is written, not only the ones the deployment
    declared: a route key that is absent falls back to
    `SONDER_PROVIDER_*_INTIMATE`, a deployment-wide setting, and the user would
    silently get the operator's model on the track they brought their own for.
    """
    chosen = next(
        (c for c in creds if c.purpose == INTIMATE and c.model and c.api_key), None
    )
    if chosen is None:
        return base

    from .configuration import RouteHop

    # The tasks with a track variant. `SAFETY_CRITICAL` is the complement and
    # is deliberately absent; naming it here would be a second place to keep
    # that rule right.
    follows_the_track = (Task.REASON, Task.GENERATE, Task.HUMANIZE)

    out = dict(base)
    for task in follows_the_track:
        out[route_key(task.value, Track.INTIMATE.value)] = [
            RouteHop(provider=chosen.provider, model=chosen.model)
        ]
    return out


def safety_provider_names(routes: dict[str, list]) -> set[str]:
    """Providers the deployment's own chains rely on for EXTRACT or CRITIC."""
    from .provider import SAFETY_CRITICAL

    critical = {task.value for task in SAFETY_CRITICAL}
    names: set[str] = set()
    for key, hops in routes.items():
        if key.split(".", 1)[0] in critical:
            names.update(hop.provider for hop in hops)
    return names


def merge_providers(
    base: dict[str, ProviderConfig], routes: dict[str, list], creds: list[UserCredential]
) -> tuple[dict[str, ProviderConfig], list[str]]:
    """The registry this user's requests see, and what they were refused.

    A user credential normally overrides a provider of the same name -- that
    is what bringing your own key for a vendor means. The exception is a
    provider this deployment's own chains use for EXTRACT or CRITIC. Letting
    it be overridden would replace a trusted endpoint with a permissive one
    underneath a safety route, and `validate_routes` would turn that into a
    startup error for every request the user makes. Refusing the override
    keeps the safety task on the credential the operator provisioned, which is
    the only thing that makes it a guarantee.
    """
    protected = safety_provider_names(routes)
    merged = dict(base)
    refused: list[str] = []
    for name, config in as_provider_configs(creds).items():
        if name in protected:
            refused.append(name)
            continue
        merged[name] = config
    return merged, refused


async def save(
    session,
    user_id: UUID,
    *,
    provider: str,
    profile: str,
    api_key: str,
    base_url: str | None = None,
    model: str | None = None,
    purpose: str = GENERAL,
) -> None:
    """Create or replace one credential.

    Replacing on the same provider name is the useful behaviour: rotating a
    key should not require knowing the row id, and the alternative -- two rows
    for one provider -- is a route that resolves differently depending on
    which one a query happened to return.
    """
    from datetime import UTC, datetime

    ciphertext, key_version = secrets.encrypt(api_key)
    existing = (
        await session.execute(
            select(UserLlmCredentialRow).where(
                UserLlmCredentialRow.user_id == user_id,
                UserLlmCredentialRow.provider == provider,
            )
        )
    ).scalar_one_or_none()

    if existing is None:
        session.add(
            UserLlmCredentialRow(
                user_id=user_id,
                provider=provider,
                profile=profile,
                base_url=base_url,
                model=model,
                purpose=purpose,
                ciphertext=ciphertext,
                key_version=key_version,
            )
        )
        return

    existing.profile = profile
    existing.base_url = base_url
    existing.model = model
    existing.purpose = purpose
    existing.ciphertext = ciphertext
    existing.key_version = key_version
    existing.updated_at = datetime.now(UTC)


async def remove(session, user_id: UUID, provider: str) -> bool:
    result = await session.execute(
        delete(UserLlmCredentialRow).where(
            UserLlmCredentialRow.user_id == user_id,
            UserLlmCredentialRow.provider == provider,
        )
    )
    return bool(result.rowcount)


async def describe_for(session, user_id: UUID) -> list[dict]:
    """What this user has configured, without the key.

    Deliberately not built on `load_for`: listing credentials should not
    decrypt them, because the one place a plaintext key is allowed to exist is
    the request that is about to use it.
    """
    rows = (
        (
            await session.execute(
                select(UserLlmCredentialRow).where(
                    UserLlmCredentialRow.user_id == user_id
                )
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "provider": row.provider,
            "profile": row.profile,
            "base_url": row.base_url,
            "model": row.model,
            "purpose": row.purpose,
            "key_version": row.key_version,
            "updated_at": row.updated_at,
        }
        for row in rows
    ]
