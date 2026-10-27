"""Agent API schemas (snake_case JSON)."""
from __future__ import annotations

import re
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

_SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class AgentRunRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    session_id: Optional[str] = Field(default=None, max_length=64)

    @field_validator("query")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("query must not be blank")
        return v

    @field_validator("session_id")
    @classmethod
    def _session_ok(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not _SESSION_RE.match(v):
            raise ValueError("session_id must match [A-Za-z0-9_-]{1,64}")
        return v


StepKind = Literal["thought", "action", "observation", "reflection", "final"]


class Step(BaseModel):
    index: int
    kind: StepKind
    content: str
    tool: Optional[str] = None
    tool_input: Optional[str] = None
    ok: bool = True
    duration_ms: float = 0.0


class GuardrailInfo(BaseModel):
    risk: float
    route: Literal["fast_allow", "deep_verify", "fast_block"]
    latency_ms: float
    decision: Literal["allow", "block"]
    deep_verified: bool = False


class LimitsEnforced(BaseModel):
    """What actually applied. timeout/memory/process_limit are OS-enforced booleans;
    network_guard is only ever "best_effort" (in-process Python guard) or "none"."""

    timeout: bool = False
    memory: bool = False
    process_limit: bool = False
    network_guard: Literal["best_effort", "none"] = "none"


class SandboxInfo(BaseModel):
    status: Literal["idle", "ok", "error", "timeout", "blocked", "memory_exceeded"] = "idle"
    duration_ms: Optional[float] = None
    limits_enforced: LimitsEnforced = Field(default_factory=LimitsEnforced)


class MemoryInfo(BaseModel):
    hit_rate: float = 0.0


from nexus_engine.services.agent.budget import TokenBudgetReport


class AgentRunResponse(BaseModel):
    session_id: str
    answer: str
    cache_hit: bool
    blocked: bool
    steps: list[Step]
    guardrail: GuardrailInfo
    sandbox: SandboxInfo
    memory: MemoryInfo
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    model: str = "gemini-3.5-flash-lite"
    budget: Optional[TokenBudgetReport] = None
    trace_id: Optional[str] = None
