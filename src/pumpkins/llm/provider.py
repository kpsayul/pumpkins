"""Provider-agnostic structured-output LLM client (docs/llm-provider-and-keys-design.md §3).

Both call sites (llm/postprocess.py, conventions/learner.py) need exactly one
operation: "system + user prompt + Pydantic schema in → parsed object + token
counts out". This module adapts that operation to the Anthropic and OpenAI
SDKs; everything else (prompts, schemas, threshold gates) stays provider-free.

`temperature` is the one sampling knob exposed, because it is the one that
changed observed behaviour: with the API default the same diff produced 0
findings on one run and 1 on the next (docs/design-history.md, Evidence). Passing
None omits the parameter so the provider's own default applies.

Auth: each SDK reads its own key from the environment (ANTHROPIC_API_KEY /
OPENAI_API_KEY) — keys are never passed around in code. SDK imports happen
lazily inside each client constructor so the --no-llm paths never pay for them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeVar

from pydantic import BaseModel

from pumpkins.config import current_provider

T = TypeVar("T", bound=BaseModel)


@dataclass
class ParsedResult(Generic[T]):
    """Uniform result shape: keeps the `tokens in=%d out=%d` logs provider-free."""

    parsed: T | None
    input_tokens: int
    output_tokens: int


class AnthropicClient:
    def __init__(self) -> None:
        import anthropic

        self._client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env

    def parse(
        self,
        *,
        model: str,
        max_tokens: int,
        system: str,
        user: str,
        schema: type[T],
        temperature: float | None = None,
    ) -> ParsedResult[T]:
        response = self._client.messages.parse(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_format=schema,
            # Omitted entirely when None so the API default stands — some models
            # reject the parameter outright, and callers that want the default
            # should not have to guess what it is.
            **({} if temperature is None else {"temperature": temperature}),
        )
        return ParsedResult(
            parsed=response.parsed_output,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )


class OpenAIClient:
    def __init__(self) -> None:
        import openai

        self._client = openai.OpenAI()  # reads OPENAI_API_KEY from env

    def parse(
        self,
        *,
        model: str,
        max_tokens: int,
        system: str,
        user: str,
        schema: type[T],
        temperature: float | None = None,
    ) -> ParsedResult[T]:
        # OpenAI has no separate system parameter — it rides in messages.
        response = self._client.beta.chat.completions.parse(
            model=model,
            max_completion_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            response_format=schema,
            **({} if temperature is None else {"temperature": temperature}),
        )
        usage = response.usage
        return ParsedResult(
            parsed=response.choices[0].message.parsed,
            input_tokens=usage.prompt_tokens if usage else 0,
            output_tokens=usage.completion_tokens if usage else 0,
        )


_CLIENTS = {"anthropic": AnthropicClient, "openai": OpenAIClient}


def get_client(provider: str | None = None) -> AnthropicClient | OpenAIClient:
    """Client for `provider` (default: the LLM_PROVIDER environment variable)."""
    return _CLIENTS[provider or current_provider()]()
