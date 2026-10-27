"""Memory API schemas."""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class ShortTermStats(BaseModel):
    turns: int
    tokens: int
    max_turns: int


class CacheStats(BaseModel):
    entries: int
    hits: int
    misses: int
    hit_rate: float


class AuditStats(BaseModel):
    records: int


class PromotionStats(BaseModel):
    short_to_audit: int
    verified_to_cache: int
    audit_to_cache: int


class MemoryStats(BaseModel):
    short_term: ShortTermStats
    semantic_cache: CacheStats
    audit: AuditStats
    promotions: PromotionStats


class AuditRecord(BaseModel):
    id: int
    session_id: str
    kind: str
    tool: Optional[str]
    content: str
    citation_hashes: list[str]
    created_at: str


class AuditResponse(BaseModel):
    records: list[AuditRecord]


class AuditChainStatus(BaseModel):
    ok: bool
    records: int
    first_bad_id: Optional[int]


class CreateSessionRequest(BaseModel):
    """Optional body for POST /memory/sessions. ``session_id`` must match [A-Za-z0-9_-]{1,64}."""

    session_id: Optional[str] = Field(default=None, max_length=64)
