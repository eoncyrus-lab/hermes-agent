"""Credentials a user brings, and the one thing they must never change.

The rule under test throughout: a user's own key may serve their requests and
may redirect their intimate track, and may **never** reach EXTRACT or CRITIC.
Those two tasks are the operator's guarantee to the person being modelled, and
somebody who can point a route at an endpoint they chose can point it at a
model that has been talked out of its refusals -- which is the failure the
whole guard exists to prevent, and a silent one.

Nothing here touches a database. What the rows look like and how they are
encrypted is `tests/test_integration.py`'s business.
"""

from __future__ import annotations

import pytest

from sonder.config import Settings
from sonder.llm import user_credentials as uc
from sonder.llm.configuration import PROFILES, ProviderConfig, RouteHop
from sonder.llm.provider import Task, Track
from sonder.llm.router import Router
from sonder.storage import secrets


def _cred(
    provider: str, *, purpose: str = uc.GENERAL, model: str = "m"
) -> uc.UserCredential:
    return uc.UserCredential(
        provider=provider,
        profile="local",
        base_url="http://127.0.0.1:8000/v1",
        model=model,
        purpose=purpose,
        api_key="sk-user",
    )


# ------------------------------------------------------------- the merge rule


def test_a_credential_for_a_safety_provider_is_refused():
    """The deployment runs EXTRACT on `deepseek`. Letting a user replace that
    provider would put their endpoint underneath a safety route, and
    `validate_routes` would then turn it into a startup error for every
    request they made."""
    routes = {
        "extract.standard": [RouteHop(provider="deepseek", model="deepseek-chat")],
        "critic.standard": [RouteHop(provider="deepseek", model="deepseek-chat")],
        "generate.standard": [RouteHop(provider="deepseek", model="deepseek-chat")],
    }
    base = {"deepseek": ProviderConfig(profile="deepseek")}

    merged, refused = uc.merge_providers(base, routes, [_cred("deepseek")])

    assert refused == ["deepseek"]
    assert merged["deepseek"].user_supplied is False


def test_a_credential_for_anything_else_is_applied():
    routes = {
        "extract.standard": [RouteHop(provider="anthropic", model="claude")],
    }
    base = {"anthropic": ProviderConfig(profile="anthropic")}

    merged, refused = uc.merge_providers(base, routes, [_cred("my-vllm")])

    assert refused == []
    assert merged["my-vllm"].user_supplied is True
    assert merged["anthropic"].user_supplied is False


def test_only_the_safety_chains_protect_a_name():
    """The rule is about what a name is *used for*, not about the name."""
    routes = {"generate.standard": [RouteHop(provider="deepseek", model="m")]}
    base = {"deepseek": ProviderConfig(profile="deepseek")}

    merged, refused = uc.merge_providers(base, routes, [_cred("deepseek")])

    assert refused == []
    assert merged["deepseek"].user_supplied is True


# ---------------------------------------------------- the intimate redirection


def test_the_intimate_redirect_never_touches_a_safety_task():
    """Extraction and criticism do not follow the track in either direction.
    Redirecting them was this function's first behaviour, and `router._guard`
    refused it with `UnsafeRouting` -- the guard catching a real mistake."""
    routes = uc.routes_with_intimate({}, [_cred("my-vllm", purpose=uc.INTIMATE)])

    assert set(routes) == {
        "reason.intimate",
        "generate.intimate",
        "humanize.intimate",
    }
    for forbidden in ("extract.intimate", "critic.intimate", "vision.intimate"):
        assert forbidden not in routes


def test_no_intimate_credential_leaves_the_routes_alone():
    base = {"generate.standard": [RouteHop(provider="op", model="m")]}
    assert uc.routes_with_intimate(base, [_cred("general-only")]) is base


def test_a_general_credential_does_not_redirect_the_track():
    """Only a credential marked intimate moves the track. Bringing a key for
    ordinary requests is not a statement about where intimate work should go."""
    base = {"generate.intimate": [RouteHop(provider="op", model="m")]}
    assert uc.routes_with_intimate(base, [_cred("general-only")]) is base


# -------------------------------------------------------------- the trust rule


def test_a_user_provider_is_permissive_whatever_its_profile_says():
    """Including the official profiles: their key still points wherever they
    typed, and the operator cannot verify it."""
    cfg = Settings(
        database_url="postgresql+asyncpg://app:test@db/sonder",
        anthropic_api_key="operator-key",
        llm_providers={
            "their-anthropic": ProviderConfig(
                profile="anthropic", api_key="user-key", user_supplied=True
            )
        },
    )
    router = Router(cfg)

    assert router.providers["their-anthropic"] is True
    assert router.providers["anthropic"] is False, "the operator's own stays trusted"


def test_a_user_provider_cannot_serve_extraction():
    from sonder.llm.router import UnsafeRouting

    cfg = Settings(
        database_url="postgresql+asyncpg://app:test@db/sonder",
        llm_providers={
            "their-anthropic": ProviderConfig(
                profile="anthropic", api_key="user-key", user_supplied=True
            )
        },
        llm_routes={
            "extract.standard": [RouteHop(provider="their-anthropic", model="claude")]
        },
    )
    with pytest.raises(UnsafeRouting, match="safety-critical"):
        Router(cfg).resolve(Task.EXTRACT, Track.STANDARD)


def test_every_profile_can_be_named_by_a_user_credential():
    """A profile the user picks has to exist, or the row is written and then
    never resolves."""
    assert set(PROFILES) == {
        "anthropic",
        "openai",
        "local",
        "minimax",
        "deepseek",
        "openrouter",
        "zen",
    }


# ------------------------------------------------------------------ the vault


@pytest.fixture
def keyed(monkeypatch):
    from cryptography.fernet import Fernet

    def _set(value):
        monkeypatch.setattr(
            secrets, "settings", lambda: type("S", (), {"vault_key": value})()
        )

    _set(Fernet.generate_key().decode())
    return _set


def test_a_round_trip(keyed):
    ciphertext, version = secrets.encrypt("sk-secret")
    assert b"sk-secret" not in ciphertext
    assert version.startswith("v1:")
    assert secrets.decrypt(ciphertext, version) == "sk-secret"


def test_a_missing_key_is_an_error_not_a_fallback(monkeypatch):
    """A credential written in the clear because a key was absent would be
    worse than a request that fails and names the variable to set."""
    monkeypatch.setattr(secrets, "settings", lambda: type("S", (), {"vault_key": None})())
    assert secrets.configured() is False
    with pytest.raises(secrets.MissingVaultKey, match="SONDER_VAULT_KEY"):
        secrets.encrypt("sk-secret")


def test_a_rotated_key_still_opens_old_rows(monkeypatch):
    """Rotation is a prepended key: the first encrypts, all of them decrypt.
    Otherwise rotating would strand every row written before it."""
    from cryptography.fernet import Fernet

    old, new = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    monkeypatch.setattr(secrets, "settings", lambda: type("S", (), {"vault_key": old})())
    ciphertext, version = secrets.encrypt("sk-secret")

    monkeypatch.setattr(
        secrets, "settings", lambda: type("S", (), {"vault_key": f"{new},{old}"})()
    )
    assert secrets.decrypt(ciphertext, version) == "sk-secret"

    rotated, _ = secrets.encrypt("another")
    monkeypatch.setattr(secrets, "settings", lambda: type("S", (), {"vault_key": old})())
    with pytest.raises(secrets.Undecryptable):
        secrets.decrypt(rotated)


def test_an_undecryptable_row_is_reported_by_name(keyed):
    with pytest.raises(secrets.Undecryptable, match="no longer in the list"):
        secrets.decrypt(b"not a fernet token", "v1:deadbeef")
