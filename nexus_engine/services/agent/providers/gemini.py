"""Google Gemini AI LLM client supporting native tool calling and SSE streaming."""
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

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"


def _convert_schema_to_gemini(schema: dict[str, Any]) -> dict[str, Any]:
    """Convert standard JSON Schema to Gemini uppercase types."""
    if not isinstance(schema, dict):
        return schema
    out: dict[str, Any] = {}
    type_map = {
        "string": "STRING",
        "integer": "INTEGER",
        "number": "NUMBER",
        "boolean": "BOOLEAN",
        "array": "ARRAY",
        "object": "OBJECT",
    }
    for k, v in schema.items():
        if k == "type" and isinstance(v, str):
            out["type"] = type_map.get(v.lower(), v.upper())
        elif k == "properties" and isinstance(v, dict):
            out["properties"] = {
                prop: _convert_schema_to_gemini(prop_val)
                for prop, prop_val in v.items()
            }
        elif k == "items" and isinstance(v, dict):
            out["items"] = _convert_schema_to_gemini(v)
        else:
            out[k] = v
    return out


def _parse_function_call(part: dict[str, Any], index: int) -> ToolCall:
    fc = part["functionCall"]
    name = fc.get("name", "")
    return ToolCall(
        id=fc.get("id") or f"call_{name}_{index}",
        name=name,
        arguments=fc.get("args") or {},
        thought_signature=part.get("thoughtSignature"),
    )


class GeminiClient:
    """Production provider adapter for Google Gemini 3.5 Flash Lite."""

    def __init__(
        self,
        api_key: str,
        model_name: str = "gemini-3.5-flash-lite",
        client: Optional[httpx.AsyncClient] = None,
        base_url: str = GEMINI_BASE_URL,
    ) -> None:
        self.api_key = api_key
        self.model_name = model_name
        self._injected_client = client
        self.base_url = base_url.rstrip("/")
        # One pooled client per event loop: an AsyncClient must not outlive the loop it was used on.
        self._clients: dict[int, tuple[asyncio.AbstractEventLoop, httpx.AsyncClient]] = {}

    def _get_client(self) -> httpx.AsyncClient:
        if self._injected_client is not None and not self._injected_client.is_closed:
            return self._injected_client
        loop = asyncio.get_running_loop()
        self._clients = {k: v for k, v in self._clients.items() if not v[0].is_closed()}
        cached = self._clients.get(id(loop))
        if cached is None or cached[0] is not loop or cached[1].is_closed:
            client = httpx.AsyncClient(timeout=30.0)
            self._clients[id(loop)] = (loop, client)
            return client
        return cached[1]

    def _headers(self) -> dict[str, str]:
        # The key travels in a header, never the URL: httpx error messages (which get logged) include the URL.
        return {"x-goog-api-key": self.api_key}

    def _build_payload(
        self,
        messages: Sequence[LLMMessage],
        tools: Optional[Sequence[ToolDefinition]] = None,
        temperature: float = 0.2,
    ) -> dict[str, Any]:
        contents: list[dict[str, Any]] = []
        system_instructions: list[str] = []

        for m in messages:
            if m.role == "system":
                system_instructions.append(m.content)
            elif m.role == "user":
                contents.append({
                    "role": "user",
                    "parts": [{"text": m.content}],
                })
            elif m.role == "assistant":
                parts: list[dict[str, Any]] = []
                if m.content:
                    parts.append({"text": m.content})
                if m.tool_calls:
                    for tc in m.tool_calls:
                        call_dict: dict[str, Any] = {
                            "name": tc.name,
                            "args": tc.arguments,
                        }
                        part_dict = {"functionCall": call_dict}
                        if getattr(tc, "thought_signature", None):
                            part_dict["thoughtSignature"] = tc.thought_signature
                        parts.append(part_dict)
                contents.append({"role": "model", "parts": parts})
            elif m.role == "tool":
                # Function response turn. Gemini pairs responses with calls by function NAME, and
                # expects all responses to one model turn's parallel calls in a single content.
                part = {
                    "functionResponse": {
                        "name": m.name or m.tool_call_id or "function_result",
                        "response": {"result": m.content},
                    }
                }
                prev = contents[-1] if contents else None
                if prev and prev["role"] == "user" and all("functionResponse" in p for p in prev["parts"]):
                    prev["parts"].append(part)
                else:
                    contents.append({"role": "user", "parts": [part]})

        payload: dict[str, Any] = {
            "contents": contents,
            "generationConfig": {
                "temperature": temperature,
            },
        }

        if system_instructions:
            payload["systemInstruction"] = {
                "parts": [{"text": "\n\n".join(system_instructions)}]
            }

        if tools:
            func_decls = []
            for t in tools:
                schema = _convert_schema_to_gemini(t.parameters)
                func_decls.append({
                    "name": t.name,
                    "description": t.description,
                    "parameters": schema,
                })
            payload["tools"] = [{"functionDeclarations": func_decls}]

        return payload

    async def chat(
        self,
        messages: Sequence[LLMMessage],
        tools: Optional[Sequence[ToolDefinition]] = None,
        temperature: float = 0.2,
    ) -> LLMResponse:
        url = f"{self.base_url}/models/{self.model_name}:generateContent"
        payload = self._build_payload(messages, tools, temperature)

        resp = await self._get_client().post(url, json=payload, headers=self._headers())
        resp.raise_for_status()
        data = resp.json()

        candidates = data.get("candidates", [])
        if not candidates:
            return LLMResponse(content="", finish_reason="empty", model=self.model_name)

        cand = candidates[0]
        content_obj = cand.get("content", {})
        parts = content_obj.get("parts", [])

        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []

        for p in parts:
            if "text" in p:
                text_parts.append(p["text"])
            if "functionCall" in p:
                tool_calls.append(_parse_function_call(p, len(tool_calls)))

        usage = data.get("usageMetadata", {})
        prompt_tokens = usage.get("promptTokenCount", 0)
        completion_tokens = usage.get("candidatesTokenCount", 0)
        finish_reason = cand.get("finishReason", "stop")

        return LLMResponse(
            content="".join(text_parts),
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            model=self.model_name,
        )

    async def stream_turn(
        self,
        messages: Sequence[LLMMessage],
        tools: Optional[Sequence[ToolDefinition]] = None,
        temperature: float = 0.2,
    ) -> AsyncIterator[StreamChunk]:
        url = f"{self.base_url}/models/{self.model_name}:streamGenerateContent?alt=sse"
        payload = self._build_payload(messages, tools, temperature)
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        usage: dict[str, Any] = {}
        finish_reason = "stop"

        async with self._get_client().stream("POST", url, json=payload, headers=self._headers()) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                raw_json = line[6:].strip()
                if not raw_json or raw_json == "[DONE]":
                    continue
                try:
                    chunk_data = json.loads(raw_json)
                except ValueError as ex:
                    log.debug("error parsing stream chunk: %s", ex)
                    continue
                usage = chunk_data.get("usageMetadata") or usage  # cumulative; the last one wins
                cands = chunk_data.get("candidates", [])
                if not cands:
                    continue
                finish_reason = cands[0].get("finishReason", finish_reason)
                for p in cands[0].get("content", {}).get("parts", []):
                    if "functionCall" in p:  # Gemini sends each call whole, never as fragments
                        tool_calls.append(_parse_function_call(p, len(tool_calls)))
                    if p.get("text"):
                        text_parts.append(p["text"])
                        yield StreamChunk(text=p["text"])

        yield StreamChunk(response=LLMResponse(
            content="".join(text_parts),
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            prompt_tokens=usage.get("promptTokenCount", 0),
            completion_tokens=usage.get("candidatesTokenCount", 0),
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
