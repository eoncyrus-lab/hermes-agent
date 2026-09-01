"""The data-driven provider registry and declared route chains.

The property under test throughout is that adding a cheap third-party vendor
cannot quietly move signal extraction or the Critic onto it. Those two tasks
exist to notice discomfort, boundaries and pressure; a model tuned not to
refuse is also tuned not to notice, and nothing about the output looks wrong
when that happens.
"""

from __future__ import annotations

import pytest

from sonder.config import Settings
from sonder.llm import Task, Track
from sonder.llm.provider import ProviderError
from sonder.llm.router import Router, UnsafeRouting

VENDORS = ["minimax", "deepseek", "openrouter", "zen"]
TEST_DATABASE_URL = "postgresql+asyncpg://app:test@db/sonder"


def _cfg(**kw) -> Settings:
    kw.setdefault("anthropic_api_key", "test")
    kw.setdefault("database_url", TEST_DATABASE_URL)
    return Settings(**kw)


def test_database_url_is_required_outside_compose(monkeypatch):
    from pydantic import ValidationError

    monkeypatch.delenv("SONDER_DATABASE_URL", raising=False)
    with pytest.raises(ValidationError, match="database_url"):
        Settings()


@pytest.mark.parametrize("vendor", VENDORS)
def test_vendor_slot_registers_from_a_single_api_key(vendor):
    r = Router(_cfg(**{f"{vendor}_api_key": "test"}))
    assert vendor in r.providers


def test_all_four_vendor_slots_can_coexist():
    r = Router(
        _cfg(
            minimax_api_key="test",
            deepseek_api_key="test",
            openrouter_api_key="test",
            zen_api_key="test",
        )
    )
    assert set(VENDORS).issubset(r.providers)


@pytest.mark.parametrize("vendor", VENDORS)
def test_every_third_party_vendor_is_permissive(vendor):
    r = Router(_cfg(**{f"{vendor}_api_key": "test"}))
    assert r.providers[vendor] is True, f"{vendor} was treated as safety-trusted"


@pytest.mark.parametrize("vendor", VENDORS)
@pytest.mark.parametrize("task", ["extract", "critic"])
def test_safety_critical_work_cannot_be_routed_to_a_vendor(vendor, task):
    cfg = _cfg(
        **{f"{vendor}_api_key": "test"},
        llm_routes={f"{task}.standard": [{"provider": vendor, "model": "m"}]},
    )
    with pytest.raises(UnsafeRouting, match="safety-critical"):
        Router(cfg).resolve(Task(task), Track.STANDARD)


@pytest.mark.parametrize("vendor", VENDORS)
def test_a_vendor_cannot_be_a_safety_critical_failover_hop(vendor):
    """A guard break anywhere in the chain is an error, not a hop to skip."""
    cfg = _cfg(
        **{f"{vendor}_api_key": "test"},
        llm_routes={
            "extract.standard": [
                {"provider": "anthropic", "model": "claude-haiku-4-5-20251001"},
                {"provider": vendor, "model": "cheap"},
            ]
        },
    )
    with pytest.raises(UnsafeRouting):
        Router(cfg).resolve(Task.EXTRACT, Track.STANDARD)


@pytest.mark.parametrize("vendor", VENDORS)
def test_a_vendor_never_inherits_a_default_model(vendor):
    """`claude-opus-5` must never be sent to DeepSeek because a slot was set."""
    cfg = _cfg(**{f"{vendor}_api_key": "test"}, provider_generate=vendor)
    with pytest.raises(ProviderError, match="carries no default model"):
        Router(cfg).resolve(Task.GENERATE, Track.STANDARD)


def test_declared_chain_resolves_in_order():
    cfg = _cfg(
        deepseek_api_key="test",
        openrouter_api_key="test",
        llm_routes={
            "generate.standard": [
                {"provider": "deepseek", "model": "deepseek-chat"},
                {"provider": "openrouter", "model": "openrouter/free"},
            ]
        },
    )
    route = Router(cfg).resolve(Task.GENERATE, Track.STANDARD)
    assert (route.provider_name, route.model) == ("deepseek", "deepseek-chat")


def test_failover_walks_the_declared_chain():
    cfg = _cfg(
        deepseek_api_key="test",
        openrouter_api_key="test",
        minimax_api_key="test",
        llm_routes={
            "generate.intimate": [
                {"provider": "deepseek", "model": "deepseek-chat"},
                {"provider": "openrouter", "model": "openrouter/free"},
                {"provider": "minimax", "model": "MiniMax-M2"},
            ]
        },
    )
    r = Router(cfg)
    first = r.resolve(Task.GENERATE, Track.INTIMATE)
    second = r._fallback(first)
    assert second is not None
    third = r._fallback(second)
    assert [hop.provider_name for hop in (first, second, third)] == [
        "deepseek",
        "openrouter",
        "minimax",
    ]
    assert r._fallback(third) is None


async def test_a_refusal_fails_over_along_the_chain():
    from sonder.llm import Completion, ContentPolicyRefusal

    class Provider:
        def __init__(self, refusal=False):
            self.refusal = refusal
            self.calls: list[str] = []

        async def complete(self, messages, **kw):
            self.calls.append(kw["model"])
            if self.refusal:
                raise ContentPolicyRefusal("policy")
            return Completion("ok", kw["model"])

    cfg = _cfg(
        deepseek_api_key="test",
        openrouter_api_key="test",
        llm_routes={
            "generate.standard": [
                {"provider": "deepseek", "model": "deepseek-chat"},
                {"provider": "openrouter", "model": "openrouter/free"},
            ]
        },
    )
    r = Router(cfg)
    refusing, serving = Provider(refusal=True), Provider()
    r._providers["deepseek"] = refusing
    r._providers["openrouter"] = serving

    out = await r.complete(Task.GENERATE, [])

    assert refusing.calls == ["deepseek-chat"]
    assert serving.calls == ["openrouter/free"]
    assert out.model == "openrouter/free"


async def test_a_transient_provider_error_fails_over_in_declared_order():
    from sonder.llm import Completion

    class Provider:
        def __init__(self, error=False):
            self.error = error
            self.calls: list[str] = []

        async def complete(self, messages, **kw):
            self.calls.append(kw["model"])
            if self.error:
                raise ProviderError("unavailable")
            return Completion("ok", kw["model"])

    cfg = _cfg(
        deepseek_api_key="test",
        openrouter_api_key="test",
        llm_routes={
            "generate.standard": [
                {"provider": "deepseek", "model": "deepseek-chat"},
                {"provider": "openrouter", "model": "openrouter/free"},
            ]
        },
    )
    r = Router(cfg)
    failing, serving = Provider(error=True), Provider()
    r._providers["deepseek"] = failing
    r._providers["openrouter"] = serving

    out = await r.complete(Task.GENERATE, [])

    assert failing.calls == ["deepseek-chat"] * 3
    assert serving.calls == ["openrouter/free"]
    assert out.model == "openrouter/free"


def test_a_hop_this_deployment_has_not_configured_is_skipped():
    cfg = _cfg(
        openrouter_api_key="test",
        llm_routes={
            "generate.standard": [
                {"provider": "zen", "model": "free-model"},
                {"provider": "openrouter", "model": "openrouter/free"},
            ]
        },
    )
    assert Router(cfg).resolve(Task.GENERATE).provider_name == "openrouter"


def test_a_chain_with_no_configured_hop_is_an_error():
    cfg = _cfg(llm_routes={"generate.standard": [{"provider": "zen", "model": "free"}]})
    with pytest.raises(ProviderError, match="has not configured"):
        Router(cfg).resolve(Task.GENERATE)


def test_a_typo_in_a_provider_name_is_an_error():
    cfg = _cfg(llm_routes={"generate.standard": [{"provider": "zne", "model": "m"}]})
    with pytest.raises(ProviderError, match="unknown provider"):
        Router(cfg).resolve(Task.GENERATE)


def test_a_route_key_that_is_not_task_dot_track_is_an_error():
    cfg = _cfg(llm_routes={"generate": [{"provider": "anthropic", "model": "m"}]})
    with pytest.raises(ProviderError, match="not 'task.track'"):
        Router(cfg).validate_routes()


def test_validate_routes_accepts_a_complete_vendor_deployment():
    cfg = _cfg(
        deepseek_api_key="test",
        minimax_api_key="test",
        llm_routes={
            "generate.standard": [{"provider": "deepseek", "model": "deepseek-chat"}],
            "generate.intimate": [{"provider": "minimax", "model": "MiniMax-M2"}],
            "reason.standard": [{"provider": "deepseek", "model": "deepseek-reasoner"}],
        },
    )
    Router(cfg).validate_routes()


def test_declared_registry_builds_a_named_provider():
    cfg = _cfg(
        llm_providers={
            "openrouter-eu": {
                "profile": "openrouter",
                "api_key": "test",
                "base_url": "https://eu.openrouter.example/api/v1",
            }
        },
        llm_routes={
            "generate.standard": [
                {"provider": "openrouter-eu", "model": "openrouter/free"}
            ]
        },
    )
    r = Router(cfg)
    assert r.providers["openrouter-eu"] is True
    assert r.resolve(Task.GENERATE).provider_name == "openrouter-eu"


def test_a_declared_provider_cannot_claim_an_unknown_profile():
    cfg = _cfg(llm_providers={"mystery": {"profile": "not-a-profile"}})
    with pytest.raises(ProviderError, match="unknown profile"):
        Router(cfg)


def test_responses_protocol_fails_fast_until_it_is_implemented():
    cfg = _cfg(
        llm_providers={
            "future": {
                "profile": "openrouter",
                "api_key": "test",
                "protocol": "responses",
            }
        }
    )
    with pytest.raises(ProviderError, match="supports only chat_completions"):
        Router(cfg)


def test_a_private_gateway_on_a_trusted_profile_loses_safety_trust():
    """The credential slot never confers trust; only the endpoint does."""
    cfg = Settings(
        database_url=TEST_DATABASE_URL,
        llm_providers={
            "proxy": {
                "profile": "anthropic",
                "api_key": "test",
                "base_url": "https://proxy.example/v1",
            }
        },
    )
    assert Router(cfg).providers["proxy"] is True


@pytest.mark.parametrize("profile", ["anthropic", "openai"])
@pytest.mark.parametrize("task", [Task.EXTRACT, Task.CRITIC])
def test_named_custom_first_party_profile_cannot_run_safety_work(profile, task):
    cfg = _cfg(
        llm_providers={
            "proxy": {
                "profile": profile,
                "api_key": "test",
                "base_url": f"https://{profile}-proxy.example/v1",
            }
        },
        llm_routes={
            f"{task.value}.standard": [{"provider": "proxy", "model": "reviewer"}]
        },
    )

    with pytest.raises(UnsafeRouting):
        Router(cfg).resolve(task, Track.STANDARD)


def test_openrouter_attribution_headers_are_sent():
    cfg = _cfg(
        openrouter_api_key="test",
        openrouter_http_referer="https://sonder.example",
        openrouter_title="Sonder",
    )
    client = Router(cfg).provider("openrouter")._client
    headers = client.default_headers
    assert headers["HTTP-Referer"] == "https://sonder.example"
    assert headers["X-OpenRouter-Title"] == "Sonder"


def test_declared_provider_without_a_key_is_disabled_not_instantiated():
    cfg = _cfg(
        llm_providers={"spare": {"profile": "openrouter"}},
        llm_routes={
            "generate.standard": [
                {"provider": "spare", "model": "openrouter/free"},
                {"provider": "anthropic", "model": "claude-opus-5"},
            ]
        },
    )
    r = Router(cfg)
    assert "spare" not in r.providers
    assert r.disabled_providers == {"spare": "missing API key"}
    assert r.resolve(Task.GENERATE).provider_name == "anthropic"


def test_vendor_base_url_override_is_honoured():
    cfg = _cfg(deepseek_api_key="test", deepseek_base_url="https://mirror.example/v1")
    client = Router(cfg).provider("deepseek")._client
    assert str(client.base_url).rstrip("/") == "https://mirror.example/v1"


def test_zen_defaults_to_its_published_chat_completions_endpoint():
    client = Router(_cfg(zen_api_key="test")).provider("zen")._client
    assert "opencode.ai" in str(client.base_url)


def test_a_declared_provider_may_override_a_convenience_slot():
    cfg = _cfg(
        zen_api_key="ignored",
        llm_providers={
            "zen": {
                "profile": "zen",
                "api_key": "test",
                "base_url": "https://zen.internal/v1",
            }
        },
    )
    r = Router(cfg)
    assert str(r.provider("zen")._client.base_url).rstrip("/") == (
        "https://zen.internal/v1"
    )
    assert r._order.count("zen") == 1, "the slot was registered twice"


async def test_a_refusal_walks_past_more_than_one_hop():
    """A three-provider chain exists because the second one may refuse too."""
    from sonder.llm import Completion, ContentPolicyRefusal

    class Provider:
        def __init__(self, refusal=False):
            self.refusal = refusal
            self.calls: list[str] = []

        async def complete(self, messages, **kw):
            self.calls.append(kw["model"])
            if self.refusal:
                raise ContentPolicyRefusal("policy")
            return Completion("ok", kw["model"])

        async def structured(self, messages, schema, **kw):
            self.calls.append(kw["model"])
            if self.refusal:
                raise ContentPolicyRefusal("policy")
            return kw["model"]

    def _router():
        cfg = _cfg(
            deepseek_api_key="test",
            openrouter_api_key="test",
            minimax_api_key="test",
            llm_routes={
                "generate.standard": [
                    {"provider": "deepseek", "model": "deepseek-chat"},
                    {"provider": "openrouter", "model": "openrouter/free"},
                    {"provider": "minimax", "model": "MiniMax-M2"},
                ]
            },
        )
        r = Router(cfg)
        r._providers["deepseek"] = Provider(refusal=True)
        r._providers["openrouter"] = Provider(refusal=True)
        r._providers["minimax"] = Provider()
        return r

    r = _router()
    out = await r.complete(Task.GENERATE, [])
    assert out.model == "MiniMax-M2"
    assert r._providers["openrouter"].calls == ["openrouter/free"]

    r = _router()
    assert await r.structured(Task.GENERATE, [], object) == "MiniMax-M2"


async def test_a_chain_that_refuses_all_the_way_down_raises():
    from sonder.llm import ContentPolicyRefusal

    class Refusing:
        async def complete(self, messages, **kw):
            raise ContentPolicyRefusal("policy")

    cfg = _cfg(
        deepseek_api_key="test",
        openrouter_api_key="test",
        llm_routes={
            "generate.standard": [
                {"provider": "deepseek", "model": "a"},
                {"provider": "openrouter", "model": "b"},
            ]
        },
    )
    r = Router(cfg)
    r._providers["deepseek"] = Refusing()
    r._providers["openrouter"] = Refusing()
    with pytest.raises(ContentPolicyRefusal):
        await r.complete(Task.GENERATE, [])


async def test_legacy_failover_between_two_providers_terminates():
    """The default deployment shape: two providers, no declared routes.

    Nothing orders them, so a failover that only remembers the provider it
    just left hands them back and forth forever — one live billed call per
    lap, and the request never returns.
    """
    from sonder.llm import ContentPolicyRefusal

    class Refusing:
        def __init__(self, name):
            self.name = name

        async def complete(self, messages, **kw):
            calls.append(self.name)
            raise ContentPolicyRefusal("policy")

        async def structured(self, messages, schema, **kw):
            calls.append(self.name)
            raise ContentPolicyRefusal("policy")

    cfg = Settings(
        database_url=TEST_DATABASE_URL,
        anthropic_api_key="test",
        openai_api_key="test",
    )

    calls: list[str] = []
    r = Router(cfg)
    r._providers = {"anthropic": Refusing("anthropic"), "openai": Refusing("openai")}
    r._order = ["anthropic", "openai"]
    r._permissive = {"anthropic": False, "openai": False}
    with pytest.raises(ContentPolicyRefusal):
        await r.complete(Task.GENERATE, [])
    assert calls == ["anthropic", "openai"], "each provider is tried exactly once"

    calls.clear()
    with pytest.raises(ContentPolicyRefusal):
        await r.structured(Task.GENERATE, [], object)
    assert calls == ["anthropic", "openai"]


# -------------------------------------------- safety trust is not configurable


@pytest.mark.parametrize("vendor", VENDORS)
@pytest.mark.parametrize("task", ["extract", "critic"])
def test_an_operator_cannot_vouch_for_a_vendor(vendor, task):
    cfg = _cfg(
        **{f"{vendor}_api_key": "test"},
        safety_trusted_providers=[vendor],
        llm_routes={f"{task}.standard": [{"provider": vendor, "model": "m"}]},
    )
    router = Router(cfg)
    assert router.providers[vendor] is True
    assert router.vouched == frozenset()
    with pytest.raises(UnsafeRouting, match="cannot be disabled"):
        router.resolve(Task(task), Track.STANDARD)


@pytest.mark.parametrize("vendor", VENDORS)
def test_naming_one_vendor_does_not_promote_another(vendor):
    cfg = _cfg(
        **{f"{v}_api_key": "test" for v in VENDORS},
        safety_trusted_providers=[vendor],
        llm_routes={
            "extract.standard": [{"provider": vendor, "model": "m"}],
        },
    )
    with pytest.raises(UnsafeRouting):
        Router(cfg).resolve(Task.EXTRACT, Track.STANDARD)


def test_the_override_is_off_by_default():
    cfg = _cfg(
        openrouter_api_key="test",
        llm_routes={"extract.standard": [{"provider": "openrouter", "model": "m"}]},
    )
    router_cfg = Router(_cfg(openrouter_api_key="test"))
    assert router_cfg.vouched == frozenset()
    with pytest.raises(UnsafeRouting):
        Router(cfg).resolve(Task.EXTRACT, Track.STANDARD)


def test_the_guard_says_configuration_cannot_disable_it():
    cfg = _cfg(
        openrouter_api_key="test",
        llm_routes={"extract.standard": [{"provider": "openrouter", "model": "m"}]},
    )
    with pytest.raises(UnsafeRouting, match="cannot be disabled"):
        Router(cfg).resolve(Task.EXTRACT, Track.STANDARD)


def test_vouching_for_an_unconfigured_provider_changes_nothing():
    cfg = _cfg(openrouter_api_key="test", safety_trusted_providers=["deepseek"])
    router = Router(cfg)
    assert router.vouched == frozenset()
    assert router.providers["openrouter"] is True


def test_a_comma_separated_list_is_accepted(monkeypatch):
    """Env vars are strings; JSON is not a friendly thing to ask an operator for."""
    import sonder.config as config

    monkeypatch.setenv("SONDER_SAFETY_TRUSTED_PROVIDERS", "openrouter, deepseek")
    monkeypatch.setenv("SONDER_DATABASE_URL", TEST_DATABASE_URL)
    config.settings.cache_clear()
    try:
        assert config.Settings().safety_trusted_providers == ["openrouter", "deepseek"]
    finally:
        config.settings.cache_clear()


def test_a_json_list_is_also_accepted(monkeypatch):
    import sonder.config as config

    monkeypatch.setenv("SONDER_SAFETY_TRUSTED_PROVIDERS", '["openrouter"]')
    monkeypatch.setenv("SONDER_DATABASE_URL", TEST_DATABASE_URL)
    config.settings.cache_clear()
    try:
        assert config.Settings().safety_trusted_providers == ["openrouter"]
    finally:
        config.settings.cache_clear()


def test_a_fully_third_party_deployment_cannot_override_safety_routes():
    chain = [{"provider": "openrouter", "model": "minimax/minimax-m3:free"}]
    cfg = Settings(
        database_url=TEST_DATABASE_URL,
        openrouter_api_key="test",
        safety_trusted_providers=["openrouter"],
        llm_routes={
            f"{task.value}.{track.value}": chain for task in Task for track in Track
        },
    )
    router = Router(cfg)
    with pytest.raises(UnsafeRouting):
        router.validate_routes()
    assert router.vouched == frozenset()


@pytest.mark.parametrize(
    ("slot", "base_url"),
    [
        ("anthropic", "https://anthropic-proxy.example/v1"),
        ("openai", "https://openai-proxy.example/v1"),
    ],
)
def test_custom_first_party_compatible_endpoint_cannot_be_promoted(slot, base_url):
    cfg = _cfg(
        **{
            f"{slot}_api_key": "test",
            f"{slot}_base_url": base_url,
        },
        safety_trusted_providers=[slot],
        llm_routes={"extract.standard": [{"provider": slot, "model": "reviewer"}]},
    )
    with pytest.raises(UnsafeRouting):
        Router(cfg).resolve(Task.EXTRACT)


def test_local_endpoint_cannot_be_promoted():
    cfg = _cfg(
        local_base_url="http://localhost:8000/v1",
        safety_trusted_providers=["local"],
        llm_routes={"critic.standard": [{"provider": "local", "model": "reviewer"}]},
    )
    with pytest.raises(UnsafeRouting):
        Router(cfg).resolve(Task.CRITIC)


def test_a_track_override_without_a_provider_is_refused():
    """A model this deployment supplies itself cannot be guessed at.

    `_auto` places a provider by model-name prefix. For an ordinary route that
    is only a hint -- `resolve` swaps in the chosen provider's own default, so
    a wrong guess costs nothing. A track override is used verbatim, so the
    guess decides where a self-hosted model name is actually sent.
    """
    cfg = _cfg(openai_api_key="test", model_generate_intimate="hermes-4-70b")
    with pytest.raises(ProviderError, match="leaves its provider on 'auto'"):
        Router(cfg).resolve(Task.GENERATE, Track.INTIMATE)


def test_a_track_override_with_a_provider_resolves():
    cfg = _cfg(
        local_base_url="http://localhost:8000/v1",
        provider_generate_intimate="local",
        model_generate_intimate="hermes-4-70b",
    )
    route = Router(cfg).resolve(Task.GENERATE, Track.INTIMATE)
    assert (route.provider_name, route.model) == ("local", "hermes-4-70b")


@pytest.mark.parametrize(
    ("slot", "expected_model"),
    [("anthropic_api_key", "claude-opus-5"), ("openai_api_key", "gpt-5")],
)
def test_a_single_first_party_deployment_still_resolves(slot, expected_model):
    """The lenient guess has to survive: without a track override the model
    name is replaced by the provider's own default anyway."""
    cfg = Settings(database_url=TEST_DATABASE_URL, **{slot: "test"})
    router = Router(cfg)
    router.validate_routes()
    assert router.resolve(Task.REASON).model == expected_model
