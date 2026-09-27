"""Tests for the AI provider abstraction layer."""

import pytest

from app.agents.providers.mock import MockAIProvider
from app.agents.providers.protocol import AIProvider, AIProviderError
from app.models.domain import AgentResult


# ── Protocol compliance ────────────────────────────────────────────────────────


def test_mock_provider_satisfies_protocol() -> None:
    """MockAIProvider must structurally satisfy the AIProvider protocol."""
    provider = MockAIProvider()
    assert isinstance(provider, AIProvider)


# ── MockAIProvider.generate ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mock_generate_returns_string() -> None:
    provider = MockAIProvider()
    result = await provider.generate("Tell me about the bug.")
    assert isinstance(result, str)
    assert len(result) > 0


@pytest.mark.asyncio
async def test_mock_generate_with_override() -> None:
    provider = MockAIProvider(response_override="custom response")
    result = await provider.generate("any prompt")
    assert result == "custom response"


@pytest.mark.asyncio
async def test_mock_generate_accepts_system_prompt() -> None:
    provider = MockAIProvider()
    result = await provider.generate(
        prompt="Investigate this.",
        system_prompt="You are a software debugging assistant.",
    )
    assert isinstance(result, str)


# ── MockAIProvider.generate_structured ────────────────────────────────────────


@pytest.mark.asyncio
async def test_mock_generate_structured_returns_agent_result() -> None:
    """generate_structured must return a valid instance of the requested schema."""
    from pydantic import BaseModel

    class SimpleSchema(BaseModel):
        value: str = "default"
        count: int = 0

    provider = MockAIProvider()
    result = await provider.generate_structured("prompt", SimpleSchema)
    assert isinstance(result, SimpleSchema)
    assert result.value == "default"


# ── MockAIProvider.generate_stream ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mock_generate_stream_yields_strings() -> None:
    provider = MockAIProvider()
    stream = await provider.generate_stream("Stream this.")
    chunks = [chunk async for chunk in stream]
    assert len(chunks) > 0
    assert all(isinstance(c, str) for c in chunks)
    full_text = "".join(chunks)
    assert len(full_text) > 0
