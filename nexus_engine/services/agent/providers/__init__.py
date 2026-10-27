"""Pluggable LLM Providers module."""
from __future__ import annotations

import os
from typing import Optional

from nexus_engine.services.agent.providers.base import (
    LLMClient,
    LLMMessage,
    LLMResponse,
    ToolCall,
    ToolDefinition,
)
from nexus_engine.services.agent.providers.gemini import GeminiClient
from nexus_engine.services.agent.providers.mock import MockLLMClient
from nexus_engine.services.agent.providers.openai_compatible import LocalOpenAIClient


def create_llm_client(
    provider: Optional[str] = None,
    api_key: Optional[str] = None,
    model_name: Optional[str] = None,
    local_url: Optional[str] = None,
) -> LLMClient:
    """Factory creating LLMClient based on provider name or environment variables."""
    p = (provider or os.environ.get("LLM_PROVIDER") or os.environ.get("COGNI_LLM_PROVIDER") or "gemini").lower()

    if p == "gemini":
        key = api_key or os.environ.get("GEMINI_API_KEY", "")
        model = model_name or os.environ.get("GEMINI_MODEL_NAME", "gemini-3.5-flash-lite")
        if not key:
            # Fall back to mock if key is missing (e.g. offline dev without env)
            return MockLLMClient()
        return GeminiClient(api_key=key, model_name=model)

    if p in ("ollama", "local", "vllm", "llama_server"):
        url = local_url or os.environ.get("LLM_LOCAL_URL") or os.environ.get("COGNI_LLM_LOCAL_URL", "http://127.0.0.1:11434/v1")
        model = model_name or os.environ.get("LLM_LOCAL_MODEL") or os.environ.get("COGNI_LLM_LOCAL_MODEL", "llama3.2:3b")
        return LocalOpenAIClient(base_url=url, model_name=model)

    return MockLLMClient()


__all__ = [
    "LLMClient",
    "LLMMessage",
    "LLMResponse",
    "ToolCall",
    "ToolDefinition",
    "GeminiClient",
    "LocalOpenAIClient",
    "MockLLMClient",
    "create_llm_client",
]
