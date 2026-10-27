"""Tier 3: persistent SQLite audit / episodic journal.

Tamper-evidence: every row stores ``prev_hash`` (the previous row's ``row_hash``) and
``row_hash = sha256(prev_hash, session_id, kind, tool, content, citation_hashes, created_at)``,
and triggers abort any UPDATE or DELETE. ``verify_chain`` recomputes the chain.

HONEST LIMITS: this makes edits *detectable and blocked at the SQL layer*; someone with write
access to the database file can still drop the triggers and re-hash the entire chain, and
truncating the newest rows is undetectable without an external anchor. It is not a
substitute for WORM storage or an externally anchored log.

Every statement is parameterised. A fresh connection is opened per operation, so instances are
thread-safe; for that reason ``:memory:`` (a separate empty DB per connection) is rejected.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

GENESIS = "0" * 64

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    tool TEXT,
    content TEXT NOT NULL,
    citation_hashes TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    row_hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_session ON audit(session_id, id);
CREATE INDEX IF NOT EXISTS idx_audit_kind ON audit(kind, content);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit
BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit
BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
CREATE TABLE IF NOT EXISTS session_owner (
    session_id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    created_at TEXT NOT NULL,
    hidden INTEGER NOT NULL DEFAULT 0
);
"""


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _row_hash(prev: str, session_id: str, kind: str, tool: Optional[str], content: str,
              citation_hashes: str, created_at: str) -> str:
    return sha256_hex(json.dumps([prev, session_id, kind, tool, content, citation_hashes, created_at]))


class PersistentAudit:
    def __init__(self, db_path: str | Path) -> None:
        path = str(db_path)
        if path.strip() == "" or path.startswith(":memory:") or "mode=memory" in path:
            raise ValueError("in-memory SQLite is not supported: each connection would see a different database")
        self.db_path = path
        self._ready = False
        self._lock = threading.Lock()

    def _connect(self) -> sqlite3.Connection:
        with self._lock:
            if not self._ready:
                Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
                with closing(sqlite3.connect(self.db_path)) as c:
                    c.executescript(SCHEMA)
                self._ready = True
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def record(
        self,
        session_id: str,
        kind: str,
        content: str,
        tool: Optional[str] = None,
        citations: Sequence[str] = (),
    ) -> int:
        """Append a record. ``citations`` are raw texts; only their sha256 is stored."""
        hashes = json.dumps([sha256_hex(c) for c in citations])
        now = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")  # serialises writers so the chain cannot fork
            try:
                last = conn.execute("SELECT row_hash FROM audit ORDER BY id DESC LIMIT 1").fetchone()
                prev = last["row_hash"] if last else GENESIS
                rh = _row_hash(prev, session_id, kind, tool, content, hashes, now)
                cur = conn.execute(
                    "INSERT INTO audit (session_id, kind, tool, content, citation_hashes, created_at,"
                    " prev_hash, row_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (session_id, kind, tool, content, hashes, now, prev, rh),
                )
                conn.execute("COMMIT")
                return int(cur.lastrowid)
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    def verify_chain(self) -> dict[str, Any]:
        """Recompute the hash chain. ``first_bad_id`` is None when intact."""
        prev = GENESIS
        n = 0
        with closing(self._connect()) as conn:
            for r in conn.execute("SELECT * FROM audit ORDER BY id ASC"):
                n += 1
                expect = _row_hash(prev, r["session_id"], r["kind"], r["tool"], r["content"],
                                   r["citation_hashes"], r["created_at"])
                if r["prev_hash"] != prev or r["row_hash"] != expect:
                    return {"ok": False, "records": n, "first_bad_id": int(r["id"])}
                prev = r["row_hash"]
        return {"ok": True, "records": n, "first_bad_id": None}

    @staticmethod
    def _row(r: sqlite3.Row) -> dict[str, Any]:
        d = dict(r)
        d["citation_hashes"] = json.loads(d["citation_hashes"])
        return d

    def list_records(
        self,
        session_id: Optional[str] = None,
        limit: int = 50,
        kinds: Optional[Sequence[str]] = None,
    ) -> list[dict[str, Any]]:
        """Newest first."""
        sql = "SELECT * FROM audit"
        where: list[str] = []
        params: list[Any] = []
        if session_id:
            where.append("session_id = ?")
            params.append(session_id)
        if kinds:
            where.append("kind IN (%s)" % ",".join("?" * len(kinds)))
            params.extend(kinds)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        with closing(self._connect()) as conn:
            return [self._row(r) for r in conn.execute(sql, params)]

    def count(self) -> int:
        with closing(self._connect()) as conn:
            return int(conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0])
    def list_sessions(self) -> list[dict[str, Any]]:
        """List distinct sessions recorded in audit with aggregate metrics."""
        sql = """
            SELECT
                session_id,
                COUNT(*) as record_count,
                MIN(created_at) as started_at,
                MAX(created_at) as last_active_at
            FROM audit
            GROUP BY session_id
            ORDER BY last_active_at DESC
        """
        with closing(self._connect()) as conn:
            rows = conn.execute(sql).fetchall()
            return [dict(r) for r in rows]


    # ---- session ownership (outside the hash chain; the audit rows themselves stay immutable) ----
    def claim_session(self, session_id: str, owner: str) -> str:
        """Bind ``session_id`` to ``owner`` if unclaimed. Returns the effective owner.

        First writer wins. A previously hidden (deleted) session is revived only by its owner.
        """
        now = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        with closing(self._connect()) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO session_owner (session_id, owner, created_at) VALUES (?, ?, ?)",
                (session_id, owner, now),
            )
            actual = conn.execute(
                "SELECT owner FROM session_owner WHERE session_id = ?", (session_id,)
            ).fetchone()["owner"]
            if actual == owner:
                conn.execute("UPDATE session_owner SET hidden = 0 WHERE session_id = ?", (session_id,))
            return str(actual)

    def get_session_owner(self, session_id: str) -> Optional[str]:
        """Owner of a visible session, or None if unowned (legacy) or hidden."""
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT owner FROM session_owner WHERE session_id = ? AND hidden = 0", (session_id,)
            ).fetchone()
            return str(row["owner"]) if row else None

    def session_owners(self) -> dict[str, str]:
        """session_id -> owner for every visible owned session."""
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT session_id, owner FROM session_owner WHERE hidden = 0").fetchall()
            return {str(r["session_id"]): str(r["owner"]) for r in rows}

    def owned_session_rows(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT session_id, owner, created_at FROM session_owner WHERE hidden = 0 ORDER BY created_at DESC"
            ).fetchall()
            return [dict(r) for r in rows]

    def hidden_sessions(self) -> set[str]:
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT session_id FROM session_owner WHERE hidden = 1").fetchall()
            return {str(r["session_id"]) for r in rows}

    def hide_session(self, session_id: str) -> bool:
        """Soft-delete: audit rows are append-only, so the session is hidden from listings instead."""
        with closing(self._connect()) as conn:
            cur = conn.execute(
                "UPDATE session_owner SET hidden = 1 WHERE session_id = ? AND hidden = 0", (session_id,)
            )
            return cur.rowcount > 0

    def has_records(self, session_id: str) -> bool:
        with closing(self._connect()) as conn:
            return conn.execute("SELECT 1 FROM audit WHERE session_id = ? LIMIT 1", (session_id,)).fetchone() is not None

    def frequent(self, kind: str, min_count: int) -> list[tuple[str, int]]:
        """(content, count) for records of ``kind`` repeated at least ``min_count`` times."""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT content, COUNT(*) AS n FROM audit WHERE kind = ?"
                " GROUP BY content HAVING n >= ? ORDER BY n DESC",
                (kind, int(min_count)),
            ).fetchall()
            return [(r["content"], int(r["n"])) for r in rows]
