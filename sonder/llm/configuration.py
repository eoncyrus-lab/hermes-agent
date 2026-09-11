"""Data-only LLM provider and route configuration models.

A profile is a small bundle of facts about a vendor that should not have to be
retyped in every deployment: which wire protocol it speaks, where it lives, and
-- the part that matters -- whether its models can be trusted with the two
safety-critical tasks.

`permissive` is a property of the vendor, not a runtime switch. Aggregators,
resellers and self-hosted endpoints serve models whose safety tuning cannot be
inspected from here, so they are untrusted for EXTRACT and CRITIC by default.
Declaring one in a deployment does not promote it: nothing an operator can put
in the environment reaches this table; see `router._guard`.

One entry departs from that default, deliberately and in code rather than in
configuration -- see the note on DeepSeek in `PROFILES`.
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
    # Set only by the credential store, never by the environment: this provider
    # came from a user's own row rather than from the deployment. It is
    # permissive whatever its profile says, because the operator cannot vouch
    # for a key they did not provision and EXTRACT and CRITIC are the
    # operator's guarantee to the person being modelled -- a user can point a
    # custom endpoint at anything at all, including a model talked out of its
    # refusals.
    user_supplied: bool = False


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
    # Third-party OpenAI-compatible vendors and aggregators. Permissive by
    # default: an aggregator does not even guarantee which weights served the
    # request, so its safety properties cannot be inspected from here.
    "minimax": Profile(
        Protocol.CHAT_COMPLETIONS, "https://api.minimax.io/v1", False, True
    ),
    # DELIBERATE DEPARTURE FROM THE DEFAULT, and the only one in this table.
    #
    # This deployment runs on DeepSeek alone, so EXTRACT and CRITIC have no
    # first-party Anthropic or OpenAI endpoint to fall back to. Rather than
    # leave the two safety-critical tasks unroutable, DeepSeek is marked
    # trusted here -- in code, under review, and pinned by
    # `test_provider_registry.py` -- instead of through an operator-settable
    # switch, which is the thing the guard exists to prevent.
    #
    # What this costs, stated plainly: the Critic is the last gate before a
    # candidate reaches the user, and it scores boundary_risk. A model that
    # declines less also notices less, and that failure is silent -- low
    # `boundary_risk` for everything still looks like a passing candidate.
    # Nothing in the output will tell you it stopped working. If a first-party
    # key ever becomes available, restore this to `permissive=True` and route
    # EXTRACT and CRITIC to that endpoint instead.
    #
    # `supports_models` is unrelated to trust and is left exactly as it was;
    # only the final `permissive` flag is changed by this deployment.
    "deepseek": Profile(
        Protocol.CHAT_COMPLETIONS,
        "https://api.deepseek.com",
        supports_models=False,
        permissive=False,
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
