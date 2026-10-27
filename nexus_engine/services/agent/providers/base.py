"""Pluggable LLM Provider protocol and common data models."""
from __future__ import annotations

from typing import Any, AsyncIterator, Optional, Protocol, Sequence
from pydantic import BaseModel, Field


class ToolDefinition(BaseModel):
    name: str
    description: str
    parameters: dict[str, Any] = Field(default_factory=dict)


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    thought_signature: Optional[str] = None


class LLMMessage(BaseModel):
    role: str  # "system" | "user" | "assistant" | "tool"
    content: str
    tool_call_id: Optional[str] = None
    tool_calls: Optional[list[ToolCall]] = None
    name: Optional[str] = None  # role="tool": the function name (Gemini matches responses by name, not id)


class LLMResponse(BaseModel):
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    finish_reason: str = "stop"
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model: str = ""


class StreamChunk(BaseModel):
    """One item of stream_turn(): a text delta, or (last item only) the assembled turn."""

    text: str = ""
    response: Optional[LLMResponse] = None  # full content, tool_calls and token usage


class LLMClient(Protocol):
    """Protocol implemented by all LLM adapters (Gemini, Ollama, Mock)."""

    async def chat(
        self,
        messages: Sequence[LLMMessage],
        tools: Optional[Sequence[ToolDefinition]] = None,
        temperature: float = 0.2,
    ) -> LLMResponse:
        """Execute a non-streaming chat turn with optional tool schemas."""
        ...

    async def stream_chat(
        self,
        messages: Sequence[LLMMessage],
        tools: Optional[Sequence[ToolDefinition]] = None,
        temperature: float = 0.2,
    ) -> AsyncIterator[str]:
        """Stream token deltas for the response."""
        ...

    def stream_turn(
        self,
        messages: Sequence[LLMMessage],
        tools: Optional[Sequence[ToolDefinition]] = None,
        temperature: float = 0.2,
    ) -> AsyncIterator[StreamChunk]:
        """Stream text deltas, then one final chunk carrying tool calls and usage."""
        ...
