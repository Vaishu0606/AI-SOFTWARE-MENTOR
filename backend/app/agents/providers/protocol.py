"""
AIProvider protocol — the abstract interface all AI providers must implement.

Design goals:
- Agents are written against this protocol only, never against a concrete provider.
- Swapping providers (watsonx.ai, OpenAI-compatible, local) requires only a config
  change and a new provider implementation — no agent code changes.
- Structured generation enables typed, parseable agent outputs.
- Streaming generation enables real-time SSE progress updates.
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


@runtime_checkable
class AIProvider(Protocol):
    """Abstract interface for AI language model providers.

    All methods are async. Implementations must not raise on transient errors
    without retrying internally — callers expect either a result or a clean
    AIProviderError.
    """

    async def generate(
        self,
        prompt: str,
        system_prompt: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 2048,
        **kwargs: Any,
    ) -> str:
        """Generate a plain-text response.

        Args:
            prompt: The user/instruction prompt.
            system_prompt: Optional system-level instruction.
            temperature: Sampling temperature (lower = more deterministic).
            max_tokens: Maximum output token budget.
            **kwargs: Provider-specific options.

        Returns:
            The generated text string.
        """
        ...

    async def generate_structured(
        self,
        prompt: str,
        schema: type[T],
        system_prompt: str | None = None,
        temperature: float = 0.1,
        max_tokens: int = 4096,
        **kwargs: Any,
    ) -> T:
        """Generate a response parsed into a typed Pydantic model.

        Args:
            prompt: The user/instruction prompt.
            schema: A Pydantic BaseModel subclass that the response must conform to.
            system_prompt: Optional system-level instruction.
            temperature: Sampling temperature (lower for structured output).
            max_tokens: Maximum output token budget.
            **kwargs: Provider-specific options.

        Returns:
            An instance of `schema` populated from the model's JSON output.
        """
        ...

    async def generate_stream(
        self,
        prompt: str,
        system_prompt: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 2048,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        """Stream a response token-by-token.

        Args:
            prompt: The user/instruction prompt.
            system_prompt: Optional system-level instruction.
            temperature: Sampling temperature.
            max_tokens: Maximum output token budget.
            **kwargs: Provider-specific options.

        Yields:
            Text chunks as they are generated.
        """
        ...


class AIProviderError(Exception):
    """Raised when an AI provider call fails after all retries."""

    def __init__(self, provider: str, message: str) -> None:
        self.provider = provider
        super().__init__(f"[{provider}] {message}")
