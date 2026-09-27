"""
Mock AI provider for development and testing.

Returns deterministic canned responses so that:
- Unit tests do not require a real AI API key.
- The application can be started locally without credentials.
- Agent output parsing logic can be tested in isolation.

This provider must never be used in production.
"""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator, TypeVar

from pydantic import BaseModel

from app.agents.providers.protocol import AIProvider, AIProviderError

T = TypeVar("T", bound=BaseModel)

logger = logging.getLogger(__name__)


class MockAIProvider:
    """Deterministic mock provider for development and testing.

    Implements the AIProvider protocol without inheriting from it —
    structural subtyping via Protocol is sufficient.
    """

    def __init__(self, response_override: str | None = None) -> None:
        """
        Args:
            response_override: If set, all generate() calls return this string.
                               Useful for targeted unit tests.
        """
        self._response_override = response_override
        logger.info("MockAIProvider initialized (development/testing only)")

    async def generate(
        self,
        prompt: str,
        system_prompt: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 2048,
        **kwargs: Any,
    ) -> str:
        """Return a canned plain-text response."""
        if self._response_override is not None:
            return self._response_override

        return (
            "Mock AI response: The investigation is proceeding. "
            "This is a placeholder response from the MockAIProvider. "
            "Configure AI_PROVIDER=watsonx in .env to use a real provider."
        )

    async def generate_structured(
        self,
        prompt: str,
        schema: type[T],
        system_prompt: str | None = None,
        temperature: float = 0.1,
        max_tokens: int = 4096,
        **kwargs: Any,
    ) -> T:
        """Return a default-constructed instance of the requested schema."""
        try:
            instance = schema.model_validate({})
            return instance
        except Exception as exc:
            raise AIProviderError(
                "mock",
                f"Could not construct default instance of {schema.__name__}: {exc}",
            ) from exc

    async def generate_stream(
        self,
        prompt: str,
        system_prompt: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 2048,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        """Yield a canned streaming response word by word."""
        words = [
            "Mock",
            " streaming",
            " response",
            " from",
            " MockAIProvider.",
            " Configure",
            " AI_PROVIDER=watsonx",
            " for",
            " a",
            " real",
            " provider.",
        ]

        async def _stream() -> AsyncIterator[str]:
            for word in words:
                yield word

        return _stream()


def _assert_protocol_compliance() -> None:
    """Static check: MockAIProvider must satisfy the AIProvider protocol."""
    assert isinstance(MockAIProvider(), AIProvider), (
        "MockAIProvider does not satisfy the AIProvider protocol"
    )
