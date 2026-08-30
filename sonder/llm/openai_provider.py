from __future__ import annotations

from .configuration import Protocol
from .provider import (
    Completion,
    ContentPolicyRefusal,
    LLMProvider,
    Msg,
    ProviderError,
    to_openai_messages,
)


class OpenAICompatProvider(LLMProvider):
    """Anything speaking the OpenAI chat API.

    OpenRouter, Together, Groq, vLLM, Ollama, LM Studio. This is what makes
    "configure a different AI API" a config change rather than a rewrite, and
    what lets a self-hosted model serve tracks a hosted vendor will not.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str | None = None,
        timeout: float = 90.0,
        name: str = "openai",
        headers: dict[str, str] | None = None,
        protocol: Protocol = Protocol.CHAT_COMPLETIONS,
    ) -> None:
        import openai

        self.name = name
        self.protocol = protocol
        if protocol is not Protocol.CHAT_COMPLETIONS:
            raise ProviderError(
                f"provider '{name}' uses protocol '{protocol.value}', but this "
                "runtime currently supports only chat_completions"
            )
        self._client = openai.AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            default_headers=headers,
        )

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
        import openai

        payload = to_openai_messages(messages)
        if system:
            payload = [{"role": "system", "content": system}, *payload]
        try:
            resp = await self._client.chat.completions.create(
                model=model,
                messages=payload,  # type: ignore[arg-type]
                max_tokens=max_tokens,
                temperature=temperature,
                stop=stop,
            )
        except openai.BadRequestError as e:
            if "content_policy" in str(e).lower() or "policy" in str(e).lower():
                raise ContentPolicyRefusal(str(e)) from e
            raise ProviderError(str(e)) from e
        except openai.APIError as e:
            raise ProviderError(str(e)) from e

        choice = resp.choices[0]
        if (
            choice.finish_reason == "content_filter"
            or getattr(choice.message, "refusal", None) is not None
        ):
            raise ContentPolicyRefusal("OpenAI response was blocked by content policy")
        usage = resp.usage
        return Completion(
            text=choice.message.content or "",
            model=resp.model,
            input_tokens=usage.prompt_tokens if usage else 0,
            output_tokens=usage.completion_tokens if usage else 0,
            stop_reason=choice.finish_reason,
        )
