from __future__ import annotations

from .provider import (
    Completion,
    ContentPolicyRefusal,
    LLMProvider,
    Msg,
    ProviderError,
    to_anthropic_messages,
)


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(
        self, api_key: str, base_url: str | None = None, timeout: float = 90.0
    ) -> None:
        import anthropic

        kw: dict = {"api_key": api_key, "timeout": timeout}
        if base_url:
            kw["base_url"] = base_url
        self._client = anthropic.AsyncAnthropic(**kw)

    async def complete(
        self,
        messages: list[Msg],
        *,
        model: str,
        system: str | None = None,
        max_tokens: int = 2048,
        temperature: float = 1.0,
        stop: list[str] | None = None,
    ) -> Completion:
        import anthropic

        # `temperature` is deliberately not forwarded. The Messages API dropped
        # it from the typed signature in anthropic 1.x -- passing it raises
        # TypeError before the request is even built, which took extraction
        # and the Critic (the two tasks that may only run here) down with it.
        # The parameter stays in this signature because the provider interface
        # is shared with the OpenAI-compatible path, which still honours it.
        #
        # This does lose something real: `structured()` asked for 0.2 to keep
        # extraction close to deterministic, and there is no longer a knob for
        # that. `output_config.effort` is the SDK's replacement and is not an
        # equivalent, so choosing a setting needs a decision and a live key.
        kw: dict = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": to_anthropic_messages(messages),
        }
        if system:
            kw["system"] = system
        if stop:
            kw["stop_sequences"] = stop
        try:
            resp = await self._client.messages.create(**kw)
        except anthropic.BadRequestError as e:
            if "policy" in str(e).lower() or "refus" in str(e).lower():
                raise ContentPolicyRefusal(str(e)) from e
            raise ProviderError(str(e)) from e
        except anthropic.APIError as e:
            raise ProviderError(str(e)) from e

        stop_details = getattr(resp, "stop_details", None)
        if (
            resp.stop_reason == "refusal"
            or getattr(stop_details, "type", None) == "refusal"
        ):
            raise ContentPolicyRefusal("Anthropic response was blocked by content policy")
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        return Completion(
            text=text,
            model=resp.model,
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
            stop_reason=resp.stop_reason,
        )
