"""Tier 2: semantic cache (cosine similarity over embeddings), LRU-capped and thread-safe.

The embedding call (network I/O for real providers) always happens OUTSIDE the cache lock, so a slow or
retrying embedder can never block another thread's lookup. Each entry records the dimension of its
embedding; entries whose dimension differs from the query's are ignored explicitly (and logged) instead of
silently scoring 0.0. A failed embedding is a cache miss, never a mock-vector comparison.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from nexus_engine.core.embeddings import EmbedderLike, EmbeddingUnavailable, cosine, default_embedder

log = logging.getLogger(__name__)


@dataclass
class CacheEntry:
    query: str
    answer: str
    embedding: np.ndarray
    source: str  # "verified" | "audit_precedent"
    citations: list[str] = field(default_factory=list)
    hits: int = 0
    dim: int = 0

    def __post_init__(self) -> None:
        if not self.dim:
            self.dim = int(len(self.embedding))


@dataclass
class CacheHit:
    entry: CacheEntry
    similarity: float


class SemanticCache:
    def __init__(
        self,
        threshold: float = 0.92,
        max_entries: int = 256,
        embedder: Optional[EmbedderLike] = None,
    ) -> None:
        if not 0.0 < threshold <= 1.0:
            raise ValueError("threshold must be in (0, 1]")
        if max_entries < 1:
            raise ValueError("max_entries must be >= 1")
        self.threshold = threshold
        self.max_entries = max_entries
        self._embedder = embedder
        self._entries: list[CacheEntry] = []  # least recently used first
        self._lock = threading.RLock()
        self.hits = 0
        self.misses = 0
        self.evictions = 0
        self.dim_mismatch_skipped = 0

    def _embed(self, text: str) -> Optional[np.ndarray]:
        """Embed WITHOUT holding the lock. None when the embedder is unavailable (treated as a miss)."""
        embedder = self._embedder if self._embedder is not None else default_embedder()
        try:
            return embedder.embed(text)
        except EmbeddingUnavailable as exc:
            log.warning("Semantic cache embedding unavailable (%s); treating as a miss", exc)
            return None

    def _best_locked(self, q: np.ndarray) -> Optional[CacheHit]:
        """Best entry for an already-embedded query. Caller holds the lock; pure CPU, no I/O."""
        best: Optional[CacheHit] = None
        skipped = 0
        qd = int(len(q))
        for e in self._entries:
            if e.dim != qd:
                skipped += 1
                continue
            sim = cosine(q, e.embedding)
            if best is None or sim > best.similarity:
                best = CacheHit(e, sim)
        if skipped:
            self.dim_mismatch_skipped += skipped
            log.warning(
                "Semantic cache ignored %d entr%s with embedding dimension != %d (stale after an embedder change)",
                skipped, "y" if skipped == 1 else "ies", qd,
            )
        return best

    def _peek_vec_locked(self, q: np.ndarray) -> Optional[CacheHit]:
        best = self._best_locked(q)
        return best if best and best.similarity >= self.threshold else None

    def peek(self, query: str) -> Optional[CacheHit]:
        """Lookup without touching counters or recency."""
        if not self._entries:
            return None
        q = self._embed(query)
        if q is None:
            return None
        with self._lock:
            return self._peek_vec_locked(q)

    def lookup(self, query: str) -> Optional[CacheHit]:
        q = self._embed(query) if self._entries else None
        with self._lock:
            hit = self._peek_vec_locked(q) if q is not None else None
            if hit:
                self.hits += 1
                hit.entry.hits += 1
                if hit.entry in self._entries:
                    self._entries.remove(hit.entry)
                    self._entries.append(hit.entry)  # most recently used
            else:
                self.misses += 1
            return hit

    def put(
        self, query: str, answer: str, source: str = "verified", citations: Optional[list[str]] = None
    ) -> bool:
        """Insert; returns False (and refreshes the answer) if a near-duplicate exists."""
        q = self._embed(query)  # once, outside the lock
        if q is None:
            return False
        with self._lock:
            existing = self._peek_vec_locked(q)
            if existing:
                existing.entry.answer = answer
                return False
            self._entries.append(CacheEntry(query, answer, q, source, list(citations or [])))
            while len(self._entries) > self.max_entries:
                self._entries.pop(0)
                self.evictions += 1
            return True

    def clear(self) -> int:
        """Drop every cached answer (knowledge base changed); returns how many were removed.

        Hit/miss counters are kept: they describe traffic, not content.
        """
        with self._lock:
            removed = len(self._entries)
            self._entries.clear()
            return removed

    def __len__(self) -> int:
        return len(self._entries)

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0

    def stats(self) -> dict[str, float | int]:
        with self._lock:
            return {
                "entries": len(self._entries),
                "hits": self.hits,
                "misses": self.misses,
                "hit_rate": round(self.hit_rate, 4),
            }
