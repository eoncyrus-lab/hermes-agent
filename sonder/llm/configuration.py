"""Data-only LLM provider and route configuration models.

A profile is a small bundle of facts about a vendor that should not have to be
retyped in every deployment: which wire protocol it speaks, where it lives, and
-- the part that matters -- whether its models can be trusted with the two
safety-critical tasks.

`permissive` is a property of the vendor, not a switch. Aggregators, resellers
and self-hosted endpoints serve models whose safety tuning cannot be inspected
from here, so they are permanently untrusted for EXTRACT and CRITIC. Declaring
one explicitly in a deployment does not promote it; see `router._guard`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class Protocol(str, Enum):
    ANTHROPIC_MESSAGES = "anthropic_messages"
    CHAT_COMPLETIONS = "chat_completions"
    RESPONSES = "responses"


class ProviderConfig(BaseModel):
    """A named provider instance.

    ``profile`` supplies safe endpoint and capability defaults. Every field can
    be made explicit for a private deployment, but doing so never promotes it
    into a trusted provider for safety-critical work.
    """

    model_config = ConfigDict(extra="forbid")

    profile: str
    api_key: str | None = None
    base_url: str | None = None
    protocol: Protocol | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    supports_models: bool | None = None


class RouteHop(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    model: str


@dataclass(frozen=True, slots=True)
class Profile:
    protocol: Protocol
    base_url: str | None
    supports_models: bool
    # True == this vendor's model safety properties cannot be verified from
    # here, so it may never serve a safety-critical task.
    permissive: bool


PROFILES: dict[str, Profile] = {
    # First-party endpoints. Trusted only while the base URL stays official --
    # the router downgrades these to permissive for a custom endpoint.
    "anthropic": Profile(Protocol.ANTHROPIC_MESSAGES, None, True, False),
    "openai": Profile(Protocol.CHAT_COMPLETIONS, None, True, False),
    # A model the operator hosts. Deliberately never safety-trusted.
    "local": Profile(Protocol.CHAT_COMPLETIONS, None, True, True),
    # Third-party OpenAI-compatible vendors and aggregators. All permissive:
    # an aggregator does not even guarantee which weights served the request.
    "minimax": Profile(
        Protocol.CHAT_COMPLETIONS, "https://api.minimax.io/v1", False, True
    ),
    "deepseek": Profile(
        Protocol.CHAT_COMPLETIONS, "https://api.deepseek.com", False, True
    ),
    "openrouter": Profile(
        Protocol.CHAT_COMPLETIONS, "https://openrouter.ai/api/v1", True, True
    ),
    "zen": Profile(Protocol.CHAT_COMPLETIONS, "https://opencode.ai/zen/v1", True, True),
}

# Providers that carry no per-task default model. Their model names are vendor
# catalogue entries that change without notice -- OpenCode Zen's free tier and
# OpenRouter's `:free` variants especially -- so a deployment must name the
# model explicitly in a route chain rather than inherit a guess from here.
EXPLICIT_MODEL_PROFILES: frozenset[str] = frozenset(
    {"minimax", "deepseek", "openrouter", "zen"}
)


def route_key(task: str, track: str) -> str:
    return f"{task}.{track}"
