"""Concrete agent tools and the registry that dispatches them."""
from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import Any, Callable, Optional

from nexus_engine.api.errors import log_and_mask
from nexus_engine.core.async_bridge import run_async
from nexus_engine.services.agent.providers.base import ToolDefinition
from nexus_engine.services.memory.manager import MemoryManager
from nexus_engine.services.network.http_client import SafeHTTPClient
from nexus_engine.services.retrieval.hybrid_retriever import HybridRetriever
from nexus_engine.services.sandbox.executor import SandboxExecutor, SandboxResult
from nexus_engine.services.sandbox.policy import refusal_reason

log = logging.getLogger(__name__)

SEARCH = "search_knowledge_graph"
SANDBOX = "execute_sandbox_code"
RECALL = "recall_memory"
WEB_SEARCH = "web_search"
HTTP_FETCH = "http_fetch"
NOTHING_EARLIER = "nothing earlier in this session yet"


@dataclass
class ToolResult:
    ok: bool
    output: str
    citations: list[str] = field(default_factory=list)
    sandbox: Optional[SandboxResult] = None
    agreeing: int = 0  # retrieval hits independently found by BOTH dense and graph
    empty: bool = False  # ok result that carries no content (e.g. recall in a fresh session)


ToolFn = Callable[[str, str], ToolResult]  # (tool_input, session_id) -> result


def _run_async(coro: Any) -> Any:
    """Run an async coroutine from synchronous tool execution on the shared background loop."""
    return run_async(coro)


class ToolRegistry:
    def __init__(
        self, tools: dict[str, ToolFn], offered: Optional[Callable[[str], bool]] = None
    ) -> None:
        self._tools = dict(tools)
        # Optional predicate: a tool that is registered (so calls get a clear refusal) but should not
        # be advertised to the LLM, e.g. the sandbox while it is disabled.
        self._offered = offered

    @property
    def names(self) -> list[str]:
        return list(self._tools)

    def has(self, name: str) -> bool:
        return name in self._tools

    def definitions(self) -> list[ToolDefinition]:
        """Schemas of the registered tools only: the LLM is never offered a tool that cannot run."""
        return [
            d for d in TOOL_DEFINITIONS
            if d.name in self._tools and (self._offered is None or self._offered(d.name))
        ]

    def call(self, name: str, tool_input: str, session_id: str) -> ToolResult:
        """Never raises: tool exceptions become failed results so the loop can self-heal."""
        fn = self._tools.get(name)
        if fn is None:
            return ToolResult(False, f"unknown tool {name!r}")
        try:
            return fn(tool_input, session_id)
        except Exception as exc:  # noqa: BLE001 - boundary: convert to observation
            _cid, message = log_and_mask(log, f"tool {name}", exc)  # raw text (paths, hosts) stays in the log
            return ToolResult(False, message)


def search_knowledge_graph(retriever: HybridRetriever) -> ToolFn:
    def run(query: str, _session: str) -> ToolResult:
        hits = retriever.search(query, top_k=3)
        if not hits:
            return ToolResult(False, "no matching passages found")
        lines = [f"[{h.path}] (via {'+'.join(h.sources)}) {h.text}" for h in hits]
        agree = sum(1 for h in hits if {"dense", "graph"} <= set(h.sources))
        return ToolResult(
            True, "\n".join(lines), [f"{h.path}: {h.text}" for h in hits], agreeing=agree
        )

    return run


def execute_sandbox_code(sandbox: SandboxExecutor) -> ToolFn:
    def run(code: str, _session: str) -> ToolResult:
        # Policy first, every call: enabled flag, then an admin-only role check that is independent of
        # the flag, then "the code is in the user's own message". A refusal never reaches the executor.
        refusal = refusal_reason(sandbox.enabled, code)
        if refusal is not None:
            log.warning("sandbox call refused: %s", refusal)
            return ToolResult(False, refusal)
        res = sandbox.run(code)
        if res.status == "ok":
            return ToolResult(True, res.stdout.strip() or "(no output)", sandbox=res)
        detail = (res.stderr.strip().splitlines() or [res.status])[-1]
        return ToolResult(False, f"sandbox {res.status}: {detail}", sandbox=res)

    return run


def recall_memory(memory: MemoryManager) -> ToolFn:
    def run(key: str, session: str) -> ToolResult:
        found = memory.recall(key, session_id=session)  # never crosses into other sessions
        if not found:
            if not memory.has_prior_turns(key, session):
                # Fresh session: nothing to recall is a valid answer, not a tool failure.
                return ToolResult(True, NOTHING_EARLIER, empty=True)
            return ToolResult(False, "nothing relevant in episodic memory")
        return ToolResult(True, "\n".join(found))

    return run


def web_search(http_client: SafeHTTPClient) -> ToolFn:
    def run(query: str, _session: str) -> ToolResult:
        try:
            results = _run_async(http_client.web_search(query))
            if not results:
                return ToolResult(False, f"no web search results found for query: {query}")
            lines = [f"[{r.get('title', '')}] {r.get('snippet', '')} ({r.get('url', '')})" for r in results]
            cits = [f"{r.get('title', 'Web')}: {r.get('url', '')}" for r in results if r.get("url")]
            return ToolResult(True, "\n".join(lines), citations=cits)
        except Exception as exc:  # noqa: BLE001 - boundary: convert to observation
            _cid, message = log_and_mask(log, "web_search", exc)
            return ToolResult(False, message)

    return run


def http_fetch(http_client: SafeHTTPClient) -> ToolFn:
    def run(url: str, _session: str) -> ToolResult:
        try:
            text = _run_async(http_client.fetch_text(url))
            return ToolResult(True, text, citations=[url])
        except Exception as exc:  # noqa: BLE001 - boundary: convert to observation
            _cid, message = log_and_mask(log, "http_fetch", exc)
            return ToolResult(False, message)

    return run


# JSON Schema Definitions for LLM Tool Calling
TOOL_DEFINITIONS: list[ToolDefinition] = [
    ToolDefinition(
        name=SEARCH,
        description="Search internal knowledge base using hybrid vector and graph retrieval.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search query or concept."}
            },
            "required": ["query"],
        },
    ),
    ToolDefinition(
        name=SANDBOX,
        description=(
            "Run a short Python snippet in the isolated sandbox. Only code the USER wrote in their own "
            "message may be run; admin-only and disabled unless the server enables it."
        ),
        parameters={
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "The python code snippet to run."}
            },
            "required": ["code"],
        },
    ),
    ToolDefinition(
        name=RECALL,
        description="Recall previous facts or conversation details from episodic memory for this session.",
        parameters={
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Key phrase or topic to recall."}
            },
            "required": ["key"],
        },
    ),
    ToolDefinition(
        name=WEB_SEARCH,
        description="Search the public web for real-time information with SSRF security protection.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The web search query."}
            },
            "required": ["query"],
        },
    ),
    ToolDefinition(
        name=HTTP_FETCH,
        description="Fetch readable content from a safe public web URL.",
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "The HTTP or HTTPS URL to fetch."}
            },
            "required": ["url"],
        },
    ),
]


# Each tool takes one string argument; this is its name in the schema (the registry gets it as tool_input).
TOOL_ARG: dict[str, str] = {d.name: d.parameters["required"][0] for d in TOOL_DEFINITIONS}


def default_registry(
    retriever: HybridRetriever,
    sandbox: SandboxExecutor,
    memory: MemoryManager,
    http_client: Optional[SafeHTTPClient] = None,
) -> ToolRegistry:
    tools: dict[str, ToolFn] = {
        SEARCH: search_knowledge_graph(retriever),
        SANDBOX: execute_sandbox_code(sandbox),
        RECALL: recall_memory(memory),
    }
    if http_client is not None:
        tools[WEB_SEARCH] = web_search(http_client)
        tools[HTTP_FETCH] = http_fetch(http_client)
    else:
        # Default fallback safe client
        client = SafeHTTPClient()
        tools[WEB_SEARCH] = web_search(client)
        tools[HTTP_FETCH] = http_fetch(client)

    return ToolRegistry(tools, offered=lambda name: name != SANDBOX or sandbox.enabled)
