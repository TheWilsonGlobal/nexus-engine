"""Database operations and batch queries for NexusDatabase."""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Optional, Sequence


def doc_scope_clause(column: str, doc_id: str) -> tuple[str, tuple[Any, ...]]:
    """SQL predicate + params matching ``doc_id`` itself and ids under ``doc_id:`` / ``doc_id/``.

    Uses literal ``substr`` equality instead of ``LIKE`` so ``_`` and ``%`` in a doc_id are
    never treated as wildcards (deleting ``a_b`` must not touch ``a-b:*``). ``column`` is a
    trusted constant supplied by callers, never user input.
    """
    n = len(doc_id) + 1
    clause = f"({column} = ? OR substr({column}, 1, {n}) = ? OR substr({column}, 1, {n}) = ?)"
    return clause, (doc_id, f"{doc_id}:", f"{doc_id}/")


def insert_or_replace_document(
    conn: sqlite3.Connection,
    doc_id: str,
    title: str,
    format: str,
    chunk_count: int,
    triple_count: int,
    entity_count: int,
    duration_ms: float,
    status: str = "indexed",
    content_hash: str = "",
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO documents (doc_id, title, format, chunk_count, triple_count, entity_count, duration_ms, status, content_hash) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (doc_id, title, format, chunk_count, triple_count, entity_count, duration_ms, status, content_hash),
    )
    conn.commit()


def get_document_by_hash(conn: sqlite3.Connection, content_hash: str) -> Optional[dict[str, Any]]:
    if not content_hash:
        return None
    row = conn.execute(
        "SELECT * FROM documents WHERE content_hash = ? AND content_hash != '' LIMIT 1", (content_hash,)
    ).fetchone()
    return dict(row) if row else None


def ensure_schema_compatibility(conn: sqlite3.Connection) -> None:
    cols = [r[1] for r in conn.execute("PRAGMA table_info(documents)").fetchall()]
    if cols and "content_hash" not in cols:
        conn.execute("ALTER TABLE documents ADD COLUMN content_hash TEXT DEFAULT ''")
    if cols:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_documents_hash ON documents(content_hash)")
    conn.commit()


def list_all_documents(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM documents ORDER BY created_at DESC").fetchall()
    results = []
    for r in rows:
        d = dict(r)
        did = d.get("doc_id", "")
        v_res = conn.execute(
            "SELECT count(*) FROM chunks c JOIN chunk_embeddings e ON c.id=e.chunk_id WHERE c.doc_id=?",
            (did,),
        ).fetchone()
        node_sql, node_params = doc_scope_clause("id", did)
        edge_sql, edge_params = doc_scope_clause("source_id", did)
        n_res = conn.execute(f"SELECT count(*) FROM graph_nodes WHERE {node_sql}", node_params).fetchone()
        e_res = conn.execute(f"SELECT count(*) FROM graph_edges WHERE {edge_sql}", edge_params).fetchone()
        d["vector_count"] = v_res[0] if v_res else 0
        d["node_count"] = n_res[0] if n_res else 0
        d["edge_count"] = e_res[0] if e_res else 0
        results.append(d)
    return results


def get_document_by_id(conn: sqlite3.Connection, doc_id: str) -> Optional[dict[str, Any]]:
    row = conn.execute("SELECT * FROM documents WHERE doc_id = ?", (doc_id,)).fetchone()
    return dict(row) if row else None


def integrity_ok(conn: sqlite3.Connection) -> bool:
    """True only when SQLite's own check finds no corruption and no foreign-key violation (read-only)."""
    try:
        quick = conn.execute("PRAGMA quick_check(1)").fetchone()
        if quick is None or quick[0] != "ok":
            return False
        return conn.execute("PRAGMA foreign_key_check").fetchone() is None
    except sqlite3.Error:
        return False


def get_stats(conn: sqlite3.Connection, db_path: str) -> dict[str, Any]:
    docs = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    embs = conn.execute("SELECT COUNT(*) FROM chunk_embeddings").fetchone()[0]
    nodes = conn.execute("SELECT COUNT(*) FROM graph_nodes").fetchone()[0]
    edges = conn.execute("SELECT COUNT(*) FROM graph_edges").fetchone()[0]
    return {
        "documents": docs,
        "chunks": chunks,
        "embeddings": embs,
        "nodes": nodes,
        "edges": edges,
        "db_path": db_path,
        "verified": integrity_ok(conn),  # computed on every call; no longer a constant
    }


def delete_doc_cascade(conn: sqlite3.Connection, doc_id: str) -> int:
    conn.execute("DELETE FROM documents WHERE doc_id = ?", (doc_id,))
    cur = conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
    count = cur.rowcount
    node_sql, node_params = doc_scope_clause("id", doc_id)
    src_sql, src_params = doc_scope_clause("source_id", doc_id)
    tgt_sql, tgt_params = doc_scope_clause("target_id", doc_id)
    conn.execute(f"DELETE FROM graph_edges WHERE {src_sql} OR {tgt_sql}", src_params + tgt_params)
    conn.execute(f"DELETE FROM graph_nodes WHERE {node_sql}", node_params)
    conn.commit()
    return count


def persist_batch(
    conn: sqlite3.Connection,
    chunks: Sequence[Any],
    nodes: dict[str, dict[str, str]],
    edges: dict[tuple[str, str, str], Any],
    embeddings: Optional[dict[str, Sequence[float]]] = None,
) -> None:
    for c in chunks:
        doc_id = getattr(c, "doc_id", None) or (c.id.split("/")[0] if "/" in c.id else c.id.split(":")[0])
        level = getattr(c, "level", "clause")
        title = getattr(c, "title", c.id)
        content = getattr(c, "text", "")
        path = getattr(c, "path", None) or getattr(c, "context_header", None) or c.id
        parent_id = getattr(c, "parent_id", None)
        conn.execute(
            "INSERT OR REPLACE INTO chunks (id, doc_id, level, title, content, path, parent_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (c.id, doc_id, level, title, content, path, parent_id),
        )
        if embeddings and c.id in embeddings:
            emb = embeddings[c.id]
            conn.execute(
                "INSERT OR REPLACE INTO chunk_embeddings (chunk_id, dimension, embedding) VALUES (?, ?, ?)",
                (c.id, len(emb), json.dumps(list(emb))),
            )

    for nid, n in nodes.items():
        conn.execute(
            "INSERT OR REPLACE INTO graph_nodes (id, label, entity_type, properties) VALUES (?, ?, ?, '{}')",
            (nid, n["label"], n["type"]),
        )

    for (s, t, r), meta in edges.items():
        weight = float(meta.get("confidence", 1.0)) if isinstance(meta, dict) else 1.0
        conn.execute(
            "INSERT OR REPLACE INTO graph_edges (source_id, target_id, relation, weight) VALUES (?, ?, ?, ?)",
            (s, t, r, weight),
        )
    conn.commit()


def fetch_all_chunks_with_embeddings(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT c.id, c.doc_id, c.level, c.title, c.content, c.path, c.parent_id, ce.embedding "
        "FROM chunks c LEFT JOIN chunk_embeddings ce ON c.id = ce.chunk_id"
    ).fetchall()
    return [dict(r) for r in rows]


def fetch_all_nodes_and_edges(conn: sqlite3.Connection) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    n_rows = conn.execute("SELECT id, label, entity_type, properties FROM graph_nodes").fetchall()
    e_rows = conn.execute("SELECT source_id, target_id, relation, weight FROM graph_edges").fetchall()
    return [dict(r) for r in n_rows], [dict(r) for r in e_rows]


def fetch_chunks_list(
    conn: sqlite3.Connection,
    limit: int = 100,
    doc_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    query = (
        "SELECT c.id, c.doc_id, c.level, c.title, c.content, c.path, c.parent_id, "
        "CASE WHEN ce.chunk_id IS NOT NULL THEN 1 ELSE 0 END as has_vector, "
        "ce.dimension "
        "FROM chunks c LEFT JOIN chunk_embeddings ce ON c.id = ce.chunk_id "
    )
    params: list[Any] = []
    if doc_id:
        query += "WHERE c.doc_id = ? "
        params.append(doc_id)
    query += "ORDER BY c.doc_id, c.id LIMIT ?"
    params.append(limit)
    rows = conn.execute(query, tuple(params)).fetchall()
    return [dict(r) for r in rows]


def backfill_documents_if_empty(conn: sqlite3.Connection) -> None:
    doc_count = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    if doc_count > 0:
        return
    rows = conn.execute(
        "SELECT doc_id, COUNT(*) as cnt, MIN(title) as sample_title FROM chunks GROUP BY doc_id"
    ).fetchall()
    for r in rows:
        did = r["doc_id"]
        title = r["sample_title"] or did.replace("_", " ").title()
        cnt = r["cnt"]
        conn.execute(
            "INSERT OR IGNORE INTO documents (doc_id, title, format, chunk_count, triple_count, entity_count, status) "
            "VALUES (?, ?, 'sample', ?, 0, 0, 'indexed')",
            (did, title, cnt),
        )
    conn.commit()


def clear_all_storage_data(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM semantic_cache")
    conn.execute("DELETE FROM graph_edges")
    conn.execute("DELETE FROM graph_nodes")
    conn.execute("DELETE FROM chunk_embeddings")
    conn.execute("DELETE FROM chunks")
    conn.execute("DELETE FROM documents")
    conn.commit()

