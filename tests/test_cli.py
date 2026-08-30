from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from sonder import cli


class _Response:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self.body


async def test_smoke_local_checks_models_before_completion(monkeypatch, capsys):
    calls = []

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["headers"]["Authorization"] == "Bearer secret-key"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url):
            calls.append(("GET", url))
            return _Response({"data": [{"id": "hermes"}]})

        async def post(self, url, json):
            calls.append(("POST", url, json["model"]))
            return _Response({"choices": [{"message": {"content": "OK"}}]})

    monkeypatch.setattr(cli.httpx, "AsyncClient", Client)
    monkeypatch.setattr(
        cli,
        "settings",
        lambda: SimpleNamespace(
            local_base_url="http://local.test/v1/",
            local_api_key="secret-key",
            model_generate_intimate="hermes",
            request_timeout_s=2,
        ),
    )

    assert await cli._smoke_local() == cli.EXIT_OK
    assert calls == [
        ("GET", "http://local.test/v1/models"),
        ("POST", "http://local.test/v1/chat/completions", "hermes"),
    ]
    assert "secret-key" not in capsys.readouterr().out


async def test_smoke_local_missing_endpoint_is_clear(monkeypatch, capsys):
    monkeypatch.setattr(
        cli,
        "settings",
        lambda: SimpleNamespace(
            local_base_url=None,
            model_generate_intimate="hermes",
        ),
    )
    assert await cli._smoke_local() == cli.EXIT_CONFIG
    assert "SONDER_LOCAL_BASE_URL" in capsys.readouterr().out


async def test_token_list_does_not_print_digest(monkeypatch, capsys):
    token_id = uuid4()
    row = SimpleNamespace(
        id=token_id,
        token_sha256="digest-must-not-appear",
        label="laptop",
        created_at=datetime.now(UTC),
        revoked_at=None,
    )

    class Result:
        def scalars(self):
            return self

        def all(self):
            return [row]

    class Session:
        async def execute(self, _query):
            return Result()

    @asynccontextmanager
    async def maintenance():
        yield Session()

    async def user_exists(_user_id):
        return True

    monkeypatch.setattr(cli, "maintenance_session", maintenance)
    monkeypatch.setattr(cli, "_user_exists", user_exists)

    assert await cli._token_list(uuid4()) == cli.EXIT_OK
    output = capsys.readouterr().out
    assert str(token_id) in output
    assert "digest-must-not-appear" not in output


async def test_token_revoke_is_idempotent(monkeypatch):
    token_id = uuid4()

    class Result:
        def all(self):
            return [SimpleNamespace(id=token_id, revoked_at=datetime.now(UTC))]

    class Session:
        calls = 0

        async def execute(self, _query):
            self.calls += 1
            return Result()

    session = Session()

    @asynccontextmanager
    async def maintenance():
        yield session

    monkeypatch.setattr(cli, "maintenance_session", maintenance)
    assert await cli._token_revoke(uuid4(), token_id) == cli.EXIT_OK
    assert session.calls == 1
