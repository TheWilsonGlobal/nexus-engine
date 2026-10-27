"""Memory manager: coordinates the three tiers and implements promotions.

Promotions
  short_to_audit    turns evicted from tier 1 are written to tier 3 as 'episodic'
  verified_to_cache a verified (corroborated) answer is inserted into tier 2
  audit_to_cache    an unverified answer repeated >= N times in tier 3 ('precedent')
                    is promoted into tier 2
"""
from __future__ import annotations

import json
import re
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Callable, Optional, Sequence

from nexus_engine.core.embeddings import tokenize
from nexus_engine.services.memory.persistent_audit import PersistentAudit
from nexus_engine.services.memory.semantic_cache import CacheHit, SemanticCache
from nexus_engine.services.memory.short_term import ShortTermMemory

SECURITY_SESSION = "security"
_RECENT_WORDS = re.compile(r"\b(earlier|previous|previously|before|last|ago|recent)\b", re.I)


class MemoryManager:
    def __init__(
        self,
        audit: PersistentAudit,
        cache: SemanticCache,
        max_turns: int = 8,
        max_tokens: int = 512,
        precedent_promote_after: int = 3,
        max_sessions: int = 1000,
        session_ttl_s: float = 3600.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.audit = audit
        self.cache = cache
        self.max_turns = max_turns
        self.max_tokens = max_tokens
        self.precedent_promote_after = precedent_promote_after
        self.max_sessions = max_sessions
        self.session_ttl_s = session_ttl_s
        self._clock = clock
        self._lock = threading.RLock()
        # session_id -> (buffer, last_used); ordered least recently used first
        self._short: "OrderedDict[str, tuple[ShortTermMemory, float]]" = OrderedDict()
        self.promotions = {"short_to_audit": 0, "verified_to_cache": 0, "audit_to_cache": 0}

    # ---- tier 1 -------------------------------------------------------
    def _drop_session(self, sid: str) -> None:
        """Remove a session, promoting its remaining turns to tier 3 so nothing is lost."""
        mem, _ = self._short.pop(sid)
        for t in mem.turns():
            self.audit.record(sid, "episodic", f"{t.role}: {t.content}")
            self.promotions["short_to_audit"] += 1

    def _prune(self) -> None:
        now = self._clock()
        for sid in [k for k, (_, ts) in self._short.items() if now - ts > self.session_ttl_s]:
            self._drop_session(sid)
        while len(self._short) > self.max_sessions:
            self._drop_session(next(iter(self._short)))

    def short_term(self, session_id: str) -> ShortTermMemory:
        with self._lock:
            self._prune()
            mem = self._short[session_id][0] if session_id in self._short else ShortTermMemory(
                self.max_turns, self.max_tokens
            )
            self._short[session_id] = (mem, self._clock())
            self._short.move_to_end(session_id)
            self._prune()
            return mem

    def add_turn(self, session_id: str, role: str, content: str) -> int:
        """Add a turn; evicted turns are promoted to tier 3. Returns #promoted."""
        with self._lock:
            evicted = self.short_term(session_id).add(role, content)
            for t in evicted:
                self.audit.record(session_id, "episodic", f"{t.role}: {t.content}")
                self.promotions["short_to_audit"] += 1
            return len(evicted)

    def context(self, session_id: str) -> str:
        return self.short_term(session_id).context()

    def list_sessions(self) -> list[dict[str, Any]]:
        with self._lock:
            self._prune()
            in_memory: dict[str, dict[str, Any]] = {}
            for sid, (mem, ts) in self._short.items():
                in_memory[sid] = {
                    "session_id": sid,
                    "active": True,
                    "turns": len(mem),
                    "tokens": mem.tokens,
                    "last_used_timestamp": ts,
                }

            audit_sessions = self.audit.list_sessions()
            combined: dict[str, dict[str, Any]] = {}
            for r in audit_sessions:
                sid = r["session_id"]
                combined[sid] = {
                    "session_id": sid,
                    "active": sid in in_memory,
                    "record_count": r.get("record_count", 0),
                    "started_at": r.get("started_at"),
                    "last_active_at": r.get("last_active_at"),
                    "turns": in_memory.get(sid, {}).get("turns", 0),
                    "tokens": in_memory.get(sid, {}).get("tokens", 0),
                }

            for sid, info in in_memory.items():
                if sid not in combined:
                    now_iso = datetime.now(timezone.utc).isoformat()
                    combined[sid] = {
                        "session_id": sid,
                        "active": True,
                        "record_count": 0,
                        "started_at": now_iso,
                        "last_active_at": now_iso,
                        "turns": info["turns"],
                        "tokens": info["tokens"],
                    }
            return sorted(combined.values(), key=lambda x: str(x.get("last_active_at", "")), reverse=True)

    def delete_session(self, session_id: str) -> bool:
        """Drop the tier-1 buffer and hide the session from listings (audit rows are append-only)."""
        with self._lock:
            had_buffer = self._short.pop(session_id, None) is not None
        hidden = self.audit.hide_session(session_id)
        return had_buffer or hidden

    # ---- session ownership ---------------------------------------------
    def claim_session(self, session_id: str, owner: str) -> str:
        return self.audit.claim_session(session_id, owner)

    def session_owner(self, session_id: str) -> Optional[str]:
        return self.audit.get_session_owner(session_id)

    def session_exists(self, session_id: str) -> bool:
        with self._lock:
            if session_id in self._short:
                return True
        if session_id in self.audit.hidden_sessions():
            return False
        return self.audit.get_session_owner(session_id) is not None or self.audit.has_records(session_id)

    def list_sessions_for(self, username: str, elevated: bool) -> list[dict[str, Any]]:
        """Sessions visible to ``username``. Elevated users (admin/auditor) see every session; others
        see only sessions they own. The 'security' pseudo-session and soft-deleted sessions are never
        listed, and unowned legacy sessions are elevated-only."""
        owners = self.audit.session_owners()
        hidden = self.audit.hidden_sessions()
        out: list[dict[str, Any]] = []
        sessions = self.list_sessions()
        seen = {s["session_id"] for s in sessions}
        for row in self.audit.owned_session_rows():  # created but nothing recorded yet
            if row["session_id"] not in seen:
                sessions.append({
                    "session_id": row["session_id"], "active": False, "record_count": 0,
                    "started_at": row["created_at"], "last_active_at": row["created_at"],
                    "turns": 0, "tokens": 0,
                })
        for s in sessions:
            sid = s["session_id"]
            if sid == SECURITY_SESSION or sid in hidden:
                continue
            owner = owners.get(sid)
            if not elevated and owner != username:
                continue
            out.append({**s, "owner": owner})
        return out


    # ---- tier 2 -------------------------------------------------------
    def lookup_cache(self, query: str) -> Optional[CacheHit]:
        return self.cache.lookup(query)

    def promote_verified(self, query: str, answer: str, citations: Sequence[str] = ()) -> bool:
        inserted = self.cache.put(query, answer, "verified", list(citations))
        if inserted:
            self.promotions["verified_to_cache"] += 1
        return inserted

    # ---- tier 3 -------------------------------------------------------
    def log(
        self,
        session_id: str,
        kind: str,
        content: str,
        tool: Optional[str] = None,
        citations: Sequence[str] = (),
    ) -> int:
        return self.audit.record(session_id, kind, content, tool, citations)

    def record_precedent(self, session_id: str, query: str, answer: str, citations: Sequence[str] = ()) -> int:
        """Log an unverified answer, then promote any precedent that became frequent."""
        self.audit.record(
            session_id, "precedent", json.dumps({"query": query, "answer": answer}), None, citations
        )
        return self.promote_frequent_precedents()

    def promote_frequent_precedents(self) -> int:
        promoted = 0
        for content, _n in self.audit.frequent("precedent", self.precedent_promote_after):
            data = json.loads(content)
            if self.cache.peek(data["query"]) is None:
                self.cache.put(data["query"], data["answer"], "audit_precedent")
                self.promotions["audit_to_cache"] += 1
                promoted += 1
        return promoted

    def has_prior_turns(self, key: str, session_id: str) -> bool:
        """True if this session holds any episodic material other than the current ``key``."""
        rows = self.audit.list_records(session_id=session_id, limit=200, kinds=("query", "answer", "episodic"))
        current = self._current_query_id(rows)
        return any(r["content"] != key and r["id"] != current for r in rows)

    @staticmethod
    def _current_query_id(rows: Sequence[dict[str, Any]]) -> Optional[int]:
        """Audit id of the in-flight query: the loop logs it first, so it is the newest 'query' row
        (rows are newest first). Excluding by id, not content, keeps it out of recall even when the
        model paraphrases the key (REV-19)."""
        for r in rows:
            if r["kind"] == "query":
                return int(r["id"])
        return None

    def recall(self, key: str, session_id: Optional[str] = None, limit: int = 3) -> list[str]:
        """Past episodic material relevant to ``key`` (never the current query itself)."""
        kinds = ("query", "answer", "episodic")
        rows = self.audit.list_records(session_id=session_id, limit=200, kinds=kinds)
        current = self._current_query_id(rows)
        rows = [r for r in rows if r["id"] != current and r["content"] != key]
        if _RECENT_WORDS.search(key):
            queries = [r["content"] for r in rows if r["kind"] == "query"]
            return queries[:limit]
        qt = set(tokenize(key))
        scored: list[tuple[int, int, str]] = []
        for r in rows:
            overlap = len(qt & set(tokenize(r["content"])))
            if overlap:
                scored.append((overlap, r["id"], f"[{r['kind']}] {r['content']}"))
        scored.sort(key=lambda s: (-s[0], -s[1]))
        if scored:
            return [s[2] for s in scored[:limit]]
        recent = [f"[{r['kind']}] {r['content']}" for r in rows if r["kind"] in ("query", "answer", "episodic")]
        return recent[-limit:] if recent else []

    # ---- stats --------------------------------------------------------
    def stats(self) -> dict[str, Any]:
        with self._lock:
            turns = sum(len(m) for m, _ in self._short.values())
            tokens = sum(m.tokens for m, _ in self._short.values())
        return {
            "short_term": {"turns": turns, "tokens": tokens, "max_turns": self.max_turns},
            "semantic_cache": self.cache.stats(),
            "audit": {"records": self.audit.count()},
            "promotions": dict(self.promotions),
        }
