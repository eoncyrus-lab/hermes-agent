"""Configuration. Everything that differs between laptop and production."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from .llm.configuration import ProviderConfig, RouteHop

REPO_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="SONDER_", extra="ignore"
    )

    # Required even for non-Compose deployments. A baked-in owner credential
    # silently bypasses the role split and RLS assumptions below.
    database_url: str
    # Separate role for the few operations that legitimately span
    # relationships: outcome scoring and artifact purging. This is the ONLY
    # role with BYPASSRLS, so a bug in a request handler cannot read across
    # relationships even if it reaches for the wrong session. Required
    # wherever it is used -- there is no fallback, because falling back to the
    # app role degrades silently rather than failing.
    maintenance_database_url: str | None = None
    # Authentication only: `sonder_identity` has no table privileges at all
    # and can execute the two auth RPCs and nothing else. The API and the bot
    # hold this instead of a BYPASSRLS credential.
    identity_database_url: str | None = None
    # Erasure and inspection, which legitimately span every relationship one
    # user has. `sonder_erase` is NOT BYPASSRLS: its policies are user-scoped,
    # so it reaches no further than the account being erased.
    erasure_database_url: str | None = None
    # The ingest/analysis pipeline. A queued job has no bearer token, so
    # `sonder_worker` is scoped by the pinned relationship alone and the
    # ownership half is checked in `worker.tasks`.
    worker_database_url: str | None = None
    # There is intentionally no cross-dyad text-reader DSN. The shared app
    # role cannot authenticate tenant ownership for such a capability.
    redis_url: str = "redis://localhost:6379"

    # --- providers. Any subset may be configured; the router degrades. ---
    anthropic_api_key: str | None = None
    anthropic_base_url: str | None = None
    openai_api_key: str | None = None
    openai_base_url: str | None = Field(
        default=None,
        description="Any OpenAI-compatible endpoint: OpenRouter, Together, "
        "vLLM, Ollama, a local model. This is the escape hatch that keeps "
        "the product from being welded to one vendor.",
    )

    # --- a third provider slot, for a model you host yourself ---
    local_api_key: str = "not-needed"
    local_base_url: str | None = Field(
        default=None,
        description="OpenAI-compatible endpoint for a self-hosted model "
        "(vLLM, Ollama, llama.cpp). Used for the intimate track.",
    )
    local_is_permissive: bool = Field(
        default=True,
        description="Deprecated compatibility flag. Local endpoints are always "
        "treated as untrusted for safety-critical routing, even when false.",
    )

    # Data-driven provider registry and ordered task.track route chains. These
    # are JSON objects in environment variables. Legacy provider/model fields
    # below are translated when a route is not present here.
    # Deprecated input retained so old environments still parse. It cannot
    # promote any provider: safety trust is code-owned, never operator-owned.
    # In particular, local endpoints, protocol-compatible proxies and vendor
    # profiles remain barred from EXTRACT and CRITIC.
    safety_trusted_providers: Annotated[list[str], NoDecode] = Field(default_factory=list)

    llm_providers: dict[str, ProviderConfig] = Field(default_factory=dict)
    llm_routes: dict[str, list[RouteHop]] = Field(default_factory=dict)

    minimax_api_key: str | None = None
    minimax_base_url: str | None = None
    deepseek_api_key: str | None = None
    deepseek_base_url: str | None = None
    openrouter_api_key: str | None = None
    openrouter_base_url: str | None = None
    openrouter_http_referer: str | None = None
    openrouter_title: str | None = None
    zen_api_key: str | None = None
    zen_base_url: str | None = None

    # --- provider per task. "auto" picks by model name prefix. ---
    provider_reason: str = "auto"
    provider_generate: str = "auto"
    provider_humanize: str = "auto"
    provider_extract: str = "auto"
    provider_vision: str = "auto"

    # --- the intimate track. Reasoning, generation and rewriting have a
    # --- variant; extraction and criticism deliberately do not.
    provider_reason_intimate: str = "auto"
    provider_generate_intimate: str = "auto"
    provider_humanize_intimate: str = "auto"
    model_reason_intimate: str | None = None
    model_generate_intimate: str | None = None
    model_humanize_intimate: str | None = None

    # --- model routing by task, not by hardcoded name at call sites ---
    model_reason: str = "claude-opus-5"
    model_generate: str = "claude-opus-5"
    model_humanize: str = "claude-opus-5"
    model_extract: str = "claude-haiku-4-5-20251001"
    model_vision: str = "claude-opus-5"

    # OpenAI needs its own model names. These are separate from the Anthropic
    # defaults above so failover never sends a Claude model name to OpenAI.
    model_openai_reason: str = "gpt-5"
    model_openai_generate: str = "gpt-5"
    model_openai_humanize: str = "gpt-5"
    model_openai_extract: str = "gpt-5-mini"
    model_openai_vision: str = "gpt-5"

    skills_dir: Path = REPO_ROOT / "skills"
    vault_key: str | None = Field(default=None, description="Fernet key, from KMS.")

    # Screenshots are deleted this many hours after successful extraction.
    artifact_ttl_hours: int = 24
    request_timeout_s: float = 90.0
    telegram_token: str | None = None
    log_level: str = "INFO"

    @field_validator("safety_trusted_providers", mode="before")
    @classmethod
    def _split_provider_names(cls, value):
        """Parse the deprecated setting without granting it any authority.

        `NoDecode` is what makes this reachable: without it pydantic-settings
        JSON-decodes the raw value before any validator runs, so a plain
        comma-separated string fails to parse rather than arriving here.
        """
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return []
            if text.startswith("["):
                import json

                return json.loads(text)
            return [part.strip() for part in text.split(",") if part.strip()]
        return value


@lru_cache
def settings() -> Settings:
    return Settings()
