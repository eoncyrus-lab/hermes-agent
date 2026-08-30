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

        kw: dict = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
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
