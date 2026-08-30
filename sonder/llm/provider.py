"""The provider boundary.

Deliberately NOT the Claude Agent SDK. The SDK owns the slow reasoning path;
this owns everything on the hot path and everything high-volume, because
those need to be routable to a different model -- a cheaper one for
extraction, a different one for the Humanizer, a different vendor entirely
for tracks that a given provider's policy will not serve.

Swapping vendors should touch this package and nothing else.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, TypeVar

from pydantic import BaseModel

from .configuration import Protocol

M = TypeVar("M", bound=BaseModel)


class Task(str, Enum):
    """What the call is for. Call sites name a task, never a model."""

    REASON = "reason"  # relationship analysis, strategy
    GENERATE = "generate"  # reply candidates
    HUMANIZE = "humanize"  # register matching -- quality-critical
    EXTRACT = "extract"  # features, signals, classification -- high volume
    CRITIC = "critic"  # last safety gate before a candidate reaches the user
    VISION = "vision"  # screenshot parsing


class Track(str, Enum):
    """Which subject matter the call is for.

    INTIMATE exists because a general-purpose hosted model will decline or
    hedge on frank discussion of sex between adults, which makes it unusable
    for that part of the product. The track routes generation to a model the
    operator hosts themselves.

    It is a routing hint and nothing more. It relaxes no rule in the domain
    layer: protected preference namespaces still cannot be inferred, consent
    still has no probabilistic state, and the Critic still gates every
    candidate.
    """

    STANDARD = "standard"
    INTIMATE = "intimate"


# Tasks whose whole job is noticing that something is wrong -- discomfort,
# boundaries, pressure, manipulation. A model tuned away from refusing tends
# also to be tuned away from noticing, so these must never reach a provider
# declared permissive. Enforced in Router.resolve, not left to convention.
SAFETY_CRITICAL: frozenset[Task] = frozenset({Task.EXTRACT, Task.CRITIC})


@dataclass(slots=True)
class Msg:
    role: str  # "user" | "assistant"
    content: str
    images: list[bytes] = field(default_factory=list)


@dataclass(slots=True)
class Completion:
    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    stop_reason: str | None = None

    @property
    def cost_units(self) -> int:
        return self.input_tokens + 4 * self.output_tokens


class ProviderError(RuntimeError):
    pass


class ContentPolicyRefusal(ProviderError):
    """The provider declined. Distinct from a transient failure.

    Surfaced as its own type so the router can fail over to a differently
    configured provider rather than retrying the same one forever.
    """


class LLMProvider(ABC):
    name: str
    protocol: Protocol

    @abstractmethod
    async def complete(
        self,
        messages: list[Msg],
        *,
        model: str,
        system: str | None = None,
        max_tokens: int = 2048,
        temperature: float = 1.0,
        stop: list[str] | None = None,
    ) -> Completion: ...

    async def structured(
        self,
        messages: list[Msg],
        schema: type[M],
        *,
        model: str,
        system: str | None = None,
        max_tokens: int = 2048,
        temperature: float = 0.2,
    ) -> M:
        """Schema-constrained output.

        Default implementation prompts for JSON and validates. Providers with
        native structured output override this.
        """
        spec = json.dumps(schema.model_json_schema(), ensure_ascii=False)
        instruction = (
            "Respond with a single JSON object conforming to this schema. "
            "No prose, no code fence.\n\n" + spec
        )
        sys_prompt = f"{system}\n\n{instruction}" if system else instruction
        out = await self.complete(
            messages,
            model=model,
            system=sys_prompt,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        return schema.model_validate_json(_extract_json(out.text))


def _extract_json(text: str) -> str:
    """Tolerate fences and leading prose without silently accepting garbage."""
    s = text.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s
        s = s.rsplit("```", 1)[0]
    start = s.find("{")
    end = s.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ProviderError(f"no JSON object in response: {text[:200]!r}")
    return s[start : end + 1]


def to_openai_messages(messages: list[Msg]) -> list[dict[str, Any]]:
    import base64

    out: list[dict[str, Any]] = []
    for m in messages:
        if not m.images:
            out.append({"role": m.role, "content": m.content})
            continue
        parts: list[dict[str, Any]] = [{"type": "text", "text": m.content}]
        for img in m.images:
            b64 = base64.b64encode(img).decode()
            parts.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{b64}"},
                }
            )
        out.append({"role": m.role, "content": parts})
    return out


def to_anthropic_messages(messages: list[Msg]) -> list[dict[str, Any]]:
    import base64

    out: list[dict[str, Any]] = []
    for m in messages:
        if not m.images:
            out.append({"role": m.role, "content": m.content})
            continue
        parts: list[dict[str, Any]] = []
        for img in m.images:
            parts.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": base64.b64encode(img).decode(),
                    },
                }
            )
        parts.append({"type": "text", "text": m.content})
        out.append({"role": m.role, "content": parts})
    return out
