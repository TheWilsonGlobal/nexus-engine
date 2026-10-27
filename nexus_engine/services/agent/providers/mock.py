"""Mock LLM Provider implementing the LLMClient protocol."""
from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Optional, Sequence

from nexus_engine.services.agent.providers.base import (
    LLMMessage,
    LLMResponse,
    StreamChunk,
    ToolDefinition,
)


class MockLLMClient:
    """Deterministic mock provider wrapping MockLLM for offline tests and local simulation."""

    def __init__(self, mock_llm: Optional[Any] = None) -> None:
        if mock_llm is None:
            from nexus_engine.services.agent.llm import MockLLM
            mock_llm = MockLLM()
        self.mock_llm = mock_llm
        self.model_name = "mock-deterministic-v1"

    async def chat(
        self,
        messages: Sequence[LLMMessage],
        tools: Optional[Sequence[ToolDefinition]] = None,
        temperature: float = 0.2,
    ) -> LLMResponse:
        # Extract the last user message
        user_content = ""
        for m in reversed(messages):
            if m.role == "user":
                user_content = m.content
                break

        # Check if query requests safety verification JSON
        if "enterprise AI safety classifier" in user_content or (
            messages and "enterprise AI safety classifier" in messages[0].content
        ):
            # Check for suspicious patterns in query
            is_suspicious = any(
                w in user_content.lower()
                for w in ["override", "drop table", "exfiltrate", "ignore previous", "bypass"]
            )
            if is_suspicious:
                resp = '{"safe": false, "risk_score": 0.85, "reason": "Suspicious directive detected"}'
            else:
                resp = '{"safe": true, "risk_score": 0.15, "reason": "Standard knowledge query"}'
            return LLMResponse(
                content=resp,
                finish_reason="stop",
                prompt_tokens=len(user_content) // 4,
                completion_tokens=len(resp) // 4,
                model=self.model_name,
            )

        # Standard simulated response
        content = f"Simulated response to: {user_content}"
        return LLMResponse(
            content=content,
            finish_reason="stop",
            prompt_tokens=len(user_content) // 4,
            completion_tokens=len(content) // 4,
            model=self.model_name,
        )

    async def stream_chat(
        self,
        messages: Sequence[LLMMessage],
        tools: Optional[Sequence[ToolDefinition]] = None,
        temperature: float = 0.2,
    ) -> AsyncIterator[str]:
        response = await self.chat(messages, tools, temperature)
        words = response.content.split()
        for i, word in enumerate(words):
            yield word + (" " if i < len(words) - 1 else "")
            await asyncio.sleep(0.005)

    async def stream_turn(
        self,
        messages: Sequence[LLMMessage],
        tools: Optional[Sequence[ToolDefinition]] = None,
        temperature: float = 0.2,
    ) -> AsyncIterator[StreamChunk]:
        response = await self.chat(messages, tools, temperature)
        words = response.content.split()
        for i, word in enumerate(words):
            yield StreamChunk(text=word + (" " if i < len(words) - 1 else ""))
        yield StreamChunk(response=response)
