"""OpenAI-compatible LLM client for Ollama, vLLM, and llama-server."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator, Optional, Sequence
import httpx

from nexus_engine.services.agent.providers.base import (
    LLMMessage,
    LLMResponse,
    StreamChunk,
    ToolCall,
    ToolDefinition,
)

log = logging.getLogger(__name__)


def _parse_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "{}")
    except ValueError:
        log.warning("model emitted non-JSON tool arguments: %.200s", raw)
        return {}
    return parsed if isinstance(parsed, dict) else {}


class LocalOpenAIClient:
    """Provider adapter for Ollama, vLLM, and llama-server using OpenAI-compatible /v1 endpoints."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:11434/v1",
        model_name: str = "llama3.2:3b",
        api_key: Optional[str] = None,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model_name = model_name
        self.api_key = api_key or "ollama"
        self._client = client
        self._client_loop: Optional[asyncio.AbstractEventLoop] = None

    async def _get_client(self) -> httpx.AsyncClient:
        loop = asyncio.get_running_loop()
        stale = self._client_loop is not None and self._client_loop is not loop
        if self._client is None or self._client.is_closed or stale:
            # An AsyncClient's pooled connections belong to one event loop; never reuse it on another.
            headers = {"Authorization": f"Bearer {self.api_key}"}
            self._client = httpx.AsyncClient(headers=headers, timeout=60.0)
        self._client_loop = loop
        return self._client

    def _build_payload(
        self,
        messages: Sequence[LLMMessage],
        tools: Optional[Sequence[ToolDefinition]] = None,
        temperature: float = 0.2,
        stream: bool = False,
    ) -> dict[str, Any]:
        formatted_messages = []
        for m in messages:
            msg_dict: dict[str, Any] = {"role": m.role, "content": m.content}
            if m.tool_calls:
                msg_dict["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments),
                        },
                    }
                    for tc in m.tool_calls
                ]
            if m.tool_call_id:
                msg_dict["tool_call_id"] = m.tool_call_id
            formatted_messages.append(msg_dict)

        payload: dict[str, Any] = {
            "model": self.model_name,
            "messages": formatted_messages,
            "temperature": temperature,
            "stream": stream,
        }
        if stream:
            payload["stream_options"] = {"include_usage": True}

        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in tools
            ]

        return payload

    async def chat(
        self,
        messages: Sequence[LLMMessage],
        tools: Optional[Sequence[ToolDefinition]] = None,
        temperature: float = 0.2,
    ) -> LLMResponse:
        client = await self._get_client()
        url = f"{self.base_url}/chat/completions"
        payload = self._build_payload(messages, tools, temperature, stream=False)

        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        data = resp.json()

        choice = data.get("choices", [{}])[0]
        msg = choice.get("message", {})
        content = msg.get("content") or ""

        tool_calls: list[ToolCall] = []
        for tc in msg.get("tool_calls", []):
            fn = tc.get("function", {})
            args = _parse_arguments(fn.get("arguments", "{}"))
            tool_calls.append(
                ToolCall(id=tc.get("id", "call_1"), name=fn.get("name", ""), arguments=args)
            )

        usage = data.get("usage", {})
        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=choice.get("finish_reason", "stop"),
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            model=self.model_name,
        )

    async def stream_turn(
        self,
        messages: Sequence[LLMMessage],
        tools: Optional[Sequence[ToolDefinition]] = None,
        temperature: float = 0.2,
    ) -> AsyncIterator[StreamChunk]:
        client = await self._get_client()
        url = f"{self.base_url}/chat/completions"
        payload = self._build_payload(messages, tools, temperature, stream=True)
        text_parts: list[str] = []
        calls: dict[int, dict[str, str]] = {}  # streamed tool calls arrive as fragments keyed by index
        usage: dict[str, Any] = {}
        finish_reason = "stop"

        async with client.stream("POST", url, json=payload) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                raw_json = line[6:].strip()
                if not raw_json or raw_json == "[DONE]":
                    continue
                try:
                    chunk = json.loads(raw_json)
                except ValueError as ex:
                    log.debug("error parsing openai stream chunk: %s", ex)
                    continue
                usage = chunk.get("usage") or usage
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                choice = choices[0]
                finish_reason = choice.get("finish_reason") or finish_reason
                delta = choice.get("delta", {})
                for frag in delta.get("tool_calls") or []:
                    acc = calls.setdefault(frag.get("index", len(calls)), {"id": "", "name": "", "arguments": ""})
                    acc["id"] = frag.get("id") or acc["id"]
                    fn = frag.get("function") or {}
                    acc["name"] += fn.get("name") or ""
                    acc["arguments"] += fn.get("arguments") or ""
                token = delta.get("content")
                if token:
                    text_parts.append(token)
                    yield StreamChunk(text=token)

        tool_calls = [
            ToolCall(id=c["id"] or f"call_{i}", name=c["name"], arguments=_parse_arguments(c["arguments"]))
            for i, c in sorted(calls.items())
        ]
        yield StreamChunk(response=LLMResponse(
            content="".join(text_parts),
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            model=self.model_name,
        ))

    async def stream_chat(
        self,
        messages: Sequence[LLMMessage],
        tools: Optional[Sequence[ToolDefinition]] = None,
        temperature: float = 0.2,
    ) -> AsyncIterator[str]:
        async for chunk in self.stream_turn(messages, tools, temperature):
            if chunk.text:
                yield chunk.text
