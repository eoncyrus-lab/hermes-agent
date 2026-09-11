from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sonder.llm import ContentPolicyRefusal
from sonder.llm.anthropic_provider import AnthropicProvider
from sonder.llm.openai_provider import OpenAICompatProvider


def _openai_provider(response):
    provider = object.__new__(OpenAICompatProvider)
    provider.name = "openai"
    create = AsyncMock(return_value=response)
    provider._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    return provider


@pytest.mark.parametrize(
    ("refusal", "finish_reason"),
    [("I cannot help with that.", "stop"), (None, "content_filter")],
)
async def test_openai_normal_response_refusal_is_policy_error(refusal, finish_reason):
    from openai.types.chat import ChatCompletion

    response = ChatCompletion.model_validate(
        {
            "id": "chatcmpl_test",
            "object": "chat.completion",
            "created": 0,
            "model": "gpt-test",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": finish_reason,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "refusal": refusal,
                    },
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 0, "total_tokens": 1},
        }
    )

    with pytest.raises(ContentPolicyRefusal, match="blocked by content policy"):
        await _openai_provider(response).complete([], model="gpt-test")


async def test_anthropic_normal_response_refusal_is_policy_error():
    from anthropic.types import Message

    response = Message.model_validate(
        {
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "content": [],
            "model": "claude-test",
            "stop_reason": "refusal",
            "stop_sequence": None,
            "stop_details": {
                "type": "refusal",
                "reason": "safety",
                "message": "request declined",
            },
            "usage": {"input_tokens": 1, "output_tokens": 0},
        }
    )
    provider = object.__new__(AnthropicProvider)
    provider._client = SimpleNamespace(
        messages=SimpleNamespace(create=AsyncMock(return_value=response))
    )

    with pytest.raises(ContentPolicyRefusal, match="blocked by content policy"):
        await provider.complete([], model="claude-test")


async def test_openai_response_refusal_triggers_anthropic_model_failover():
    from openai.types.chat import ChatCompletion

    from sonder.config import Settings
    from sonder.llm import Completion, Task
    from sonder.llm.router import Router

    response = ChatCompletion.model_validate(
        {
            "id": "chatcmpl_blocked",
            "object": "chat.completion",
            "created": 0,
            "model": "gpt-primary",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "content_filter",
                    "message": {"role": "assistant", "content": None},
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 0, "total_tokens": 1},
        }
    )

    class HostedFallback:
        def __init__(self):
            self.models = []

        async def complete(self, messages, **kw):
            self.models.append(kw["model"])
            return Completion("ok", kw["model"])

    fallback = HostedFallback()
    router = Router(
        Settings(
            database_url="postgresql+asyncpg://app:test@db/sonder",
            openai_api_key="test",
            anthropic_api_key="test",
            provider_generate="openai",
            model_openai_generate="gpt-primary",
            model_generate="claude-fallback",
        )
    )
    router._providers = {
        "openai": _openai_provider(response),
        "anthropic": fallback,
    }
    router._order = ["openai", "anthropic"]
    router._permissive = {"openai": False, "anthropic": False}

    result = await router.complete(Task.GENERATE, [])

    assert fallback.models == ["claude-fallback"]
    assert result.model == "claude-fallback"


async def test_a_budget_exhausted_by_reasoning_is_not_reported_as_success():
    """The failure that cost an afternoon: a reasoning model spends max_tokens
    on its reasoning before emitting a character, so `content` comes back empty
    with `finish_reason == "length"`.

    Downstream that read as two different things and neither named the cause.
    `structured()` said "no JSON object in response: ''", and a plain
    `complete()` said nothing at all -- the humanizer's "a rewrite that grew is
    a failed rewrite, keep the original" fallback then restored the draft, so
    every rewrite silently became a no-op.
    """
    from openai.types.chat import ChatCompletion

    from sonder.llm import ProviderError

    response = ChatCompletion.model_validate(
        {
            "id": "chatcmpl_test",
            "object": "chat.completion",
            "created": 0,
            "model": "deepseek-flash",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "length",
                    "message": {"role": "assistant", "content": ""},
                }
            ],
            "usage": {"prompt_tokens": 40, "completion_tokens": 700, "total_tokens": 740},
        }
    )

    with pytest.raises(ProviderError, match="truncated at max_tokens=700"):
        await _openai_provider(response).complete(
            [], model="deepseek-flash", max_tokens=700
        )


async def test_a_truncated_but_nonempty_completion_is_still_returned():
    """Truncation alone is not the error -- callers already handle partial
    text, and the Critic truncates its `note` field rather than rejecting the
    scores. Only the empty case is indistinguishable from a provider fault."""
    from openai.types.chat import ChatCompletion

    response = ChatCompletion.model_validate(
        {
            "id": "chatcmpl_test",
            "object": "chat.completion",
            "created": 0,
            "model": "deepseek-flash",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "length",
                    "message": {"role": "assistant", "content": "partial answer"},
                }
            ],
            "usage": {"prompt_tokens": 40, "completion_tokens": 700, "total_tokens": 740},
        }
    )

    out = await _openai_provider(response).complete([], model="deepseek-flash")

    assert out.text == "partial answer"
