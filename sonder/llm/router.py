"""Task x Track -> (provider, model), with a safety guard and failover.

Call sites name a Task and, where it matters, a Track. They never name a
model and never name a vendor. Four things this buys:

  * extraction runs on a cheap model without touching call sites
  * the Humanizer can move to a different model, or a fine-tune, alone
  * the intimate track can run on a model the operator hosts, because a
    hosted general-purpose model will hedge or decline on frank discussion
    of sex between adults and is therefore unusable for that part
  * a ContentPolicyRefusal fails over rather than dead-ending

And one thing it refuses to do: send safety-critical work to a permissive
provider. See `_guard`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import structlog
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from .configuration import (
    EXPLICIT_MODEL_PROFILES,
    PROFILES,
    Protocol,
    ProviderConfig,
    route_key,
)
from .provider import (
    SAFETY_CRITICAL,
    Completion,
    ContentPolicyRefusal,
    LLMProvider,
    Msg,
    ProviderError,
    Task,
    Track,
)

if TYPE_CHECKING:  # `sonder.config` imports this package; see Router.__init__
    from ..config import Settings

log = structlog.get_logger(__name__)

# Vendors with a first-class credential slot in Settings, so a deployment can
# add one with a single environment variable instead of a JSON registry. Order
# is only the registration order; it never decides a route.
_VENDOR_SLOTS: tuple[str, ...] = ("minimax", "deepseek", "openrouter", "zen")


class UnsafeRouting(RuntimeError):
    """A safety-critical task was pointed at a permissive provider.

    Raised at resolve time rather than logged, because the failure it
    prevents is silent: a compliant model scoring `boundary_risk` returns
    low numbers for everything, the Critic passes candidates it should
    block, and nothing in the output looks wrong.
    """


@dataclass(frozen=True, slots=True)
class Route:
    task: Task
    track: Track
    provider_name: str
    model: str
    permissive: bool


class Router:
    def __init__(self, cfg: Settings | None = None) -> None:
        # Imported here, not at module scope: `sonder.config` imports this
        # package for its provider-configuration models, and importing it back
        # at module scope makes the cycle depend on which module loads first.
        from ..config import settings

        self.cfg = cfg or settings()
        self._providers: dict[str, LLMProvider] = {}
        self._permissive: dict[str, bool] = {}
        # Providers the operator named in safety_trusted_providers AND that
        # would otherwise have been permissive -- i.e. the ones where the
        # restriction was actually lifted, not merely named.
        self._vouched: set[str] = set()
        self._profiles: dict[str, str] = {}
        self._disabled: dict[str, str] = {}
        self._order: list[str] = []
        self._build()

    # ------------------------------------------------------------ providers

    def _build(self) -> None:
        cfg = self.cfg
        if cfg.anthropic_api_key:
            from .anthropic_provider import AnthropicProvider

            self._profiles["anthropic"] = "anthropic"
            self._register(
                "anthropic",
                AnthropicProvider(
                    cfg.anthropic_api_key,
                    cfg.anthropic_base_url,
                    cfg.request_timeout_s,
                ),
                # A custom endpoint can serve any model. Its safety properties
                # cannot be inferred from the Anthropic-compatible protocol.
                permissive=_is_custom_endpoint("anthropic", cfg.anthropic_base_url),
            )
        if cfg.openai_api_key:
            from .openai_provider import OpenAICompatProvider

            self._profiles["openai"] = "openai"
            self._register(
                "openai",
                OpenAICompatProvider(
                    cfg.openai_api_key, cfg.openai_base_url, cfg.request_timeout_s
                ),
                # OpenAI-compatible gateways and self-hosted endpoints are
                # unknown models, regardless of the credential slot used.
                permissive=_is_custom_endpoint("openai", cfg.openai_base_url),
            )
        if cfg.local_base_url:
            from .openai_provider import OpenAICompatProvider

            self._profiles["local"] = "local"
            self._register(
                "local",
                OpenAICompatProvider(
                    cfg.local_api_key,
                    cfg.local_base_url,
                    cfg.request_timeout_s,
                    name="local",
                ),
                # Trust is derived from the endpoint, never operator-declared.
                # Keep local_is_permissive only as a backwards-compatible
                # setting; false cannot promote a local model into safety work.
                permissive=True,
            )
        for name in _VENDOR_SLOTS:
            api_key = getattr(cfg, f"{name}_api_key", None)
            if not api_key:
                continue
            self._register_declared(
                name,
                ProviderConfig(
                    profile=name,
                    api_key=api_key,
                    base_url=getattr(cfg, f"{name}_base_url", None),
                ),
            )
        # The declared registry is last so a deployment can redefine any of the
        # convenience slots above -- a private OpenRouter gateway, say.
        for name, declared in cfg.llm_providers.items():
            self._register_declared(name, declared)
        if not self._providers:
            log.warning("no_llm_provider_configured")

    def _register_declared(self, name: str, declared: ProviderConfig) -> None:
        """Build one provider from a profile plus deployment overrides."""
        profile = PROFILES.get(declared.profile)
        if profile is None:
            raise ProviderError(
                f"provider '{name}' names unknown profile '{declared.profile}'; "
                f"known profiles: {', '.join(sorted(PROFILES))}"
            )
        protocol = declared.protocol or profile.protocol
        base_url = declared.base_url or profile.base_url
        headers = {**self._profile_headers(declared.profile), **declared.headers}
        api_key = declared.api_key or getattr(
            self.cfg, f"{declared.profile}_api_key", None
        )
        if not api_key:
            # A missing credential disables only this provider. Keep the
            # reason so `llm validate` can distinguish it from a typo.
            if name in self._providers:
                self._providers.pop(name)
                self._permissive.pop(name)
                self._profiles.pop(name)
                self._order.remove(name)
            self._disabled[name] = "missing API key"
            return
        if protocol is Protocol.ANTHROPIC_MESSAGES:
            from .anthropic_provider import AnthropicProvider

            provider: LLMProvider = AnthropicProvider(
                api_key, base_url, self.cfg.request_timeout_s
            )
            provider.name = name
        else:
            from .openai_provider import OpenAICompatProvider

            provider = OpenAICompatProvider(
                api_key,
                base_url,
                self.cfg.request_timeout_s,
                name=name,
                headers=headers or None,
                protocol=protocol,
            )
        # Trust comes from the profile and the endpoint, never from the
        # deployment: an operator cannot declare a reseller safety-trusted.
        self._profiles[name] = declared.profile
        self._register(
            name,
            provider,
            permissive=profile.permissive
            or _is_custom_endpoint(declared.profile, base_url),
        )

    def _profile_headers(self, profile_name: str) -> dict[str, str]:
        """Attribution headers OpenRouter asks integrators to send."""
        if profile_name != "openrouter":
            return {}
        cfg = self.cfg
        out: dict[str, str] = {}
        if cfg.openrouter_http_referer:
            out["HTTP-Referer"] = cfg.openrouter_http_referer
        if cfg.openrouter_title:
            out["X-OpenRouter-Title"] = cfg.openrouter_title
        return out

    def _register(self, name: str, provider: LLMProvider, *, permissive: bool) -> None:
        # Every registration path funnels through here, so the operator
        # override is applied once rather than in each of them.
        if permissive and name in self._operator_trusted:
            log.warning(
                "operator_trusted_provider",
                provider=name,
                note=(
                    "extraction and the Critic may now run on a provider whose "
                    "model safety properties cannot be verified; a model tuned "
                    "not to refuse is also tuned not to notice"
                ),
            )
            permissive = False
            self._vouched.add(name)
        if name in self._providers:
            self._order.remove(name)
        self._providers[name] = provider
        self._permissive[name] = permissive
        self._order.append(name)

    @property
    def _operator_trusted(self) -> frozenset[str]:
        return frozenset(self.cfg.safety_trusted_providers)

    @property
    def vouched(self) -> frozenset[str]:
        """Providers serving safety-critical work only because of an override."""
        return frozenset(self._vouched)

    @property
    def providers(self) -> dict[str, bool]:
        """Registered provider name -> whether it is permissive."""
        return dict(self._permissive)

    def provider(self, name: str) -> LLMProvider:
        try:
            return self._providers[name]
        except KeyError:
            detail = self._disabled.get(name)
            if detail:
                raise ProviderError(f"provider '{name}' is disabled: {detail}") from None
            raise ProviderError(
                f"provider '{name}' is not configured in this deployment"
            ) from None

    @property
    def disabled_providers(self) -> dict[str, str]:
        return dict(self._disabled)

    # -------------------------------------------------------------- routing

    def model_for(
        self,
        task: Task,
        track: Track = Track.STANDARD,
        provider_name: str | None = None,
    ) -> str:
        cfg = self.cfg
        if track is Track.INTIMATE:
            override = {
                Task.REASON: cfg.model_reason_intimate,
                Task.GENERATE: cfg.model_generate_intimate,
                Task.HUMANIZE: cfg.model_humanize_intimate,
            }.get(task)
            if override:
                return override
        model = self._provider_default_model(task, provider_name)
        if model is not None:
            return model
        # A local primary route without a track override retains the legacy
        # provider-independent model setting. Local is never offered as a
        # fallback without an explicit deployable model name.
        default = self._provider_default_model(task, "anthropic")
        assert default is not None
        return default

    def needs_explicit_model(self, provider_name: str) -> bool:
        """True when this provider has no safe per-task default model.

        Third-party catalogues churn -- OpenRouter's `:free` variants and
        OpenCode Zen's free tier especially -- and an inherited default would
        quietly send a Claude or GPT model name to a vendor that never had it.
        Those providers are reachable only through a declared route chain.
        """
        return self._profiles.get(provider_name) in EXPLICIT_MODEL_PROFILES

    def _provider_default_model(
        self, task: Task, provider_name: str | None
    ) -> str | None:
        cfg = self.cfg
        if provider_name is not None and self.needs_explicit_model(provider_name):
            return None
        if provider_name == "openai":
            return {
                Task.REASON: cfg.model_openai_reason,
                Task.GENERATE: cfg.model_openai_generate,
                Task.HUMANIZE: cfg.model_openai_humanize,
                Task.EXTRACT: cfg.model_openai_extract,
                Task.CRITIC: cfg.model_openai_extract,
                Task.VISION: cfg.model_openai_vision,
            }[task]
        if provider_name == "local":
            return None
        return {
            Task.REASON: cfg.model_reason,
            Task.GENERATE: cfg.model_generate,
            Task.HUMANIZE: cfg.model_humanize,
            Task.EXTRACT: cfg.model_extract,
            Task.CRITIC: cfg.model_extract,
            Task.VISION: cfg.model_vision,
        }[task]

    def _configured_provider(self, task: Task, track: Track) -> str:
        cfg = self.cfg
        if track is Track.INTIMATE:
            override = {
                Task.REASON: cfg.provider_reason_intimate,
                Task.GENERATE: cfg.provider_generate_intimate,
                Task.HUMANIZE: cfg.provider_humanize_intimate,
            }.get(task, "auto")
            if override and override != "auto":
                return override
        return {
            Task.REASON: cfg.provider_reason,
            Task.GENERATE: cfg.provider_generate,
            Task.HUMANIZE: cfg.provider_humanize,
            Task.EXTRACT: cfg.provider_extract,
            Task.CRITIC: cfg.provider_extract,
            Task.VISION: cfg.provider_vision,
        }[task]

    def _auto(self, model: str) -> str:
        preferred = "anthropic" if model.startswith("claude") else "openai"
        if preferred in self._providers:
            return preferred
        if not self._order:
            raise ProviderError("no LLM provider configured; set an API key")
        return self._order[0]

    def chain(self, task: Task, track: Track) -> list[Route]:
        """The declared, ordered failover chain for one route, if any.

        A hop naming a provider this deployment has not configured is skipped
        -- that is how a subset of vendors stays a valid deployment. A hop that
        breaks the safety guard is NOT skipped: listing a permissive provider
        for extraction or criticism is a mistake to surface, not to route
        around silently.
        """
        key = route_key(task.value, track.value)
        hops = self.cfg.llm_routes.get(key)
        if not hops:
            return []
        routes: list[Route] = []
        for hop in hops:
            if hop.provider not in self._providers:
                if hop.provider not in PROFILES and hop.provider not in (
                    self.cfg.llm_providers
                ):
                    raise ProviderError(
                        f"route '{key}' names unknown provider '{hop.provider}'"
                    )
                continue
            candidate = Route(
                task, track, hop.provider, hop.model, self._permissive[hop.provider]
            )
            _guard(candidate)
            routes.append(candidate)
        if not routes:
            disabled = sorted(
                hop.provider for hop in hops if hop.provider in self._disabled
            )
            detail = f" (disabled: {', '.join(disabled)})" if disabled else ""
            raise ProviderError(
                f"route '{key}' names only providers this deployment has not "
                f"configured{detail}"
            )
        return routes

    def resolve(self, task: Task, track: Track = Track.STANDARD) -> Route:
        declared = self.chain(task, track)
        if declared:
            return declared[0]
        model = self.model_for(task, track)
        name = self._configured_provider(task, track)
        if name == "auto":
            name = self._auto(model)
        if name not in self._providers:
            raise ProviderError(f"provider '{name}' is configured but has no key")
        if self.needs_explicit_model(name):
            raise ProviderError(
                f"provider '{name}' carries no default model; name it in an "
                f"llm_routes chain for '{route_key(task.value, track.value)}'"
            )
        provider_model = self.model_for(task, track, name)
        route = Route(task, track, name, provider_model, self._permissive[name])
        _guard(route)
        return route

    def validate_routes(self) -> None:
        """Resolve every public route so unsafe configuration fails early."""
        known = {route_key(t.value, k.value) for t in Task for k in Track}
        unknown = sorted(set(self.cfg.llm_routes) - known)
        if unknown:
            raise ProviderError(
                "llm_routes has keys that are not 'task.track': " + ", ".join(unknown)
            )
        if not self._providers:
            return
        for task in Task:
            self.resolve(task, Track.STANDARD)
            self.resolve(task, Track.INTIMATE)

    # ------------------------------------------------------------- calling

    @retry(
        retry=retry_if_exception(
            lambda exc: (
                isinstance(exc, ProviderError)
                and not isinstance(exc, ContentPolicyRefusal)
            )
        ),
        stop=stop_after_attempt(3),
        wait=wait_exponential(min=1, max=8),
        reraise=True,
    )
    async def _call(self, provider: LLMProvider, messages: list[Msg], **kw):
        return await provider.complete(messages, **kw)

    @retry(
        retry=retry_if_exception(
            lambda exc: (
                isinstance(exc, ProviderError)
                and not isinstance(exc, ContentPolicyRefusal)
            )
        ),
        stop=stop_after_attempt(3),
        wait=wait_exponential(min=1, max=8),
        reraise=True,
    )
    async def _structured_call(
        self, provider: LLMProvider, messages: list[Msg], schema, **kw
    ):
        return await provider.structured(messages, schema, **kw)

    async def complete(
        self,
        task: Task,
        messages: list[Msg],
        *,
        track: Track = Track.STANDARD,
        system: str | None = None,
        model: str | None = None,
        max_tokens: int = 2048,
        temperature: float = 1.0,
    ) -> Completion:
        _guard_model_override(task, model)
        route = self.resolve(task, track)
        chosen = model or route.model
        # Walk the whole chain, not one hop of it: a three-provider chain
        # exists precisely because the second one may refuse too. Each
        # provider is tried at most once, so this terminates.
        tried: set[str] = set()
        while True:
            tried.add(route.provider_name)
            try:
                out = await self._call(
                    self._providers[route.provider_name],
                    messages,
                    model=chosen,
                    system=system,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
                break
            except ProviderError:
                fallback = self._fallback(route, frozenset(tried))
                if fallback is None:
                    raise
                log.info(
                    "provider_failover",
                    frm=route.provider_name,
                    to=fallback.provider_name,
                    task=task.value,
                    track=track.value,
                )
                route, chosen = fallback, fallback.model
        log.debug(
            "llm_call",
            task=task.value,
            track=track.value,
            model=out.model,
            cost_units=out.cost_units,
        )
        return out

    def _fallback(
        self, route: Route, tried: frozenset[str] = frozenset()
    ) -> Route | None:
        """Another provider for the same work, still subject to the guard.

        `tried` is what makes this terminate. The legacy path picks from an
        unordered set of configured providers, so without it two providers
        that both refuse hand each other back and forth forever -- one live
        billed call per lap, and the request never returns.
        """
        declared = self.chain(route.task, route.track)
        if declared:
            passed = False
            for candidate in declared:
                if candidate.provider_name == route.provider_name:
                    passed = True
                    continue
                if passed and candidate.provider_name not in tried:
                    return candidate
            return None
        for name in self._order:
            if name == route.provider_name or name in tried:
                continue
            model = self._provider_default_model(route.task, name)
            if model is None:
                continue
            candidate = Route(
                route.task,
                route.track,
                name,
                model,
                self._permissive[name],
            )
            try:
                _guard(candidate)
            except UnsafeRouting:
                continue
            return candidate
        return None

    async def structured(
        self,
        task: Task,
        messages: list[Msg],
        schema,
        *,
        track: Track = Track.STANDARD,
        **kw,
    ):
        override = kw.pop("model", None)
        _guard_model_override(task, override)
        route = self.resolve(task, track)
        model = override or route.model
        tried: set[str] = set()
        while True:
            tried.add(route.provider_name)
            try:
                return await self._structured_call(
                    self._providers[route.provider_name],
                    messages,
                    schema,
                    model=model,
                    **kw,
                )
            except ProviderError:
                fallback = self._fallback(route, frozenset(tried))
                if fallback is None:
                    raise
                log.info(
                    "provider_failover",
                    frm=route.provider_name,
                    to=fallback.provider_name,
                    task=task.value,
                    track=track.value,
                )
                route, model = fallback, fallback.model


def _guard(route: Route) -> None:
    if route.task in SAFETY_CRITICAL and route.permissive:
        raise UnsafeRouting(
            f"task '{route.task.value}' is safety-critical and cannot run on "
            f"provider '{route.provider_name}', whose model safety properties "
            "are not trusted. "
            "Signal extraction and criticism exist to notice discomfort, "
            "boundaries and pressure; a model trained not to refuse is also "
            "trained not to notice, and the resulting failure is silent. "
            f"If you accept that risk, name it in "
            f"SONDER_SAFETY_TRUSTED_PROVIDERS."
        )


def _guard_model_override(task: Task, model: str | None) -> None:
    if task in SAFETY_CRITICAL and model is not None:
        raise UnsafeRouting(
            f"task '{task.value}' is safety-critical and cannot accept a "
            "call-level model override; configure its guarded route instead"
        )


_OFFICIAL_HOSTS = {
    "anthropic": "api.anthropic.com",
    "openai": "api.openai.com",
}


def _official_host(profile_name: str) -> str | None:
    if profile_name in _OFFICIAL_HOSTS:
        return _OFFICIAL_HOSTS[profile_name]
    profile = PROFILES.get(profile_name)
    if profile is None or profile.base_url is None:
        return None
    return urlsplit(profile.base_url).hostname


def _is_custom_endpoint(profile_name: str, base_url: str | None) -> bool:
    """True when the endpoint is not the profile's own published host.

    Only meaningful for the two safety-trusted profiles; a permissive profile
    is already untrusted wherever it points.
    """
    if base_url is None:
        return False
    official_host = _official_host(profile_name)
    if official_host is None:
        return True
    parsed = urlsplit(base_url)
    return (
        parsed.scheme != "https"
        or parsed.hostname != official_host
        or parsed.port not in (None, 443)
    )


_router: Router | None = None


def router() -> Router:
    global _router
    if _router is None:
        candidate = Router()
        candidate.validate_routes()
        _router = candidate
    return _router


def reset_router() -> None:
    """Test seam: rebuild on the next call after settings change."""
    global _router
    _router = None
