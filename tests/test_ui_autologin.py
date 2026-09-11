"""The browser page's auto-login gate.

`/ui` injects a live bearer token for the configured account. That makes the
conditions under which it is handed out more important than the feature
itself, so they are pinned here rather than left to the endpoint's docstring.

The interesting case is DNS rebinding, and it is the reason the client address
alone is not enough: a page on `evil.example` can point that name at 127.0.0.1,
making its fetches same-origin, and the request then genuinely does arrive from
127.0.0.1. Only the Host header still says `evil.example`.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from sonder.api import ui as ui_module

TOKEN = "sk-sonder-test-token"


def _request(host: str | None, client: str | None) -> SimpleNamespace:
    """The two attributes `_may_auto_login` reads, and nothing else."""
    return SimpleNamespace(
        headers={} if host is None else {"host": host},
        client=None if client is None else SimpleNamespace(host=client),
    )


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(ui_module, "settings", lambda: SimpleNamespace(ui_token=TOKEN))


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1:8000", "localhost:8000", "[::1]:8000", "LOCALHOST", "127.0.0.1"],
)
def test_a_loopback_host_from_a_loopback_client_is_allowed(configured, host):
    assert ui_module._may_auto_login(_request(host, "127.0.0.1")) is True


@pytest.mark.parametrize(
    "host",
    [
        "evil.example",
        "127.0.0.1.evil.com",
        "localhost.evil.com",
        "0.0.0.0",
        "",
    ],
)
def test_a_non_loopback_host_is_refused_even_from_a_loopback_client(configured, host):
    assert ui_module._may_auto_login(_request(host, "127.0.0.1")) is False


def test_a_missing_host_header_is_refused(configured):
    assert ui_module._may_auto_login(_request(None, "127.0.0.1")) is False


def test_a_non_loopback_client_is_refused_even_with_a_loopback_host(configured):
    """The other half: a request proxied in from elsewhere can forge the Host
    header, so the peer address has to agree."""
    assert ui_module._may_auto_login(_request("127.0.0.1:8000", "10.0.0.7")) is False


def test_a_missing_client_is_refused(configured):
    assert ui_module._may_auto_login(_request("127.0.0.1:8000", None)) is False


def test_an_unset_token_disables_the_feature(monkeypatch):
    monkeypatch.setattr(ui_module, "settings", lambda: SimpleNamespace(ui_token=None))
    assert ui_module._may_auto_login(_request("127.0.0.1:8000", "127.0.0.1")) is False


async def test_the_page_carries_no_token_when_the_request_is_refused(configured):
    """The injected form is `<script>window.__SONDER_TOKEN__="..."`, and the
    page's own JS also mentions the name -- so the assertion matches the `=`
    that only the injected form has."""
    body = (await ui_module.ui(_request("evil.example", "127.0.0.1"))).body
    assert b"window.__SONDER_TOKEN__=" not in body
    assert b"<!--autologin-->" in body


async def test_the_page_carries_the_token_for_a_loopback_request(configured):
    response = await ui_module.ui(_request("127.0.0.1:8000", "127.0.0.1"))
    assert f'window.__SONDER_TOKEN__="{TOKEN}"'.encode() in response.body
    assert response.headers["cache-control"] == "no-store"


# ------------------------------------- creating a relationship from the page


def test_a_third_party_profile_cannot_be_created_without_an_expiry():
    """The schema requires `retention_until` for a third-party subject, so no
    request may leave it unset -- there is a default, and zero is refused.

    The endpoint exists so that "no relationships exist" is not answered with
    instructions to run a CLI command. That answer previously shipped with a
    `<your user_id>` placeholder, which is exactly the thing a first-run user
    has no reason to know.
    """
    from pydantic import ValidationError

    assert ui_module.RelationshipIn(display_name="x").retention_days == 365
    with pytest.raises(ValidationError):
        ui_module.RelationshipIn(display_name="x", retention_days=0)
    with pytest.raises(ValidationError):
        ui_module.RelationshipIn(display_name="", retention_days=30)
