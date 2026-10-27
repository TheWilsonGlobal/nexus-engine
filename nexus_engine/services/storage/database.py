"""Unified embedded storage subsystem for Nexus-Engine.

Stores Chunks, Vector Embeddings, Knowledge Graph Nodes & Edges, and Semantic Cache
inside an ACID-compliant embedded SQLite database (data/db/nexus.db) with Recursive CTE graph traversal.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from typing import Any, Optional, Sequence

from nexus_engine.services.storage.operations import (
    backfill_documents_if_empty,
    clear_all_storage_data,
    delete_doc_cascade,
    ensure_schema_compatibility,
    fetch_all_chunks_with_embeddings,
    fetch_all_nodes_and_edges,
    fetch_chunks_list,
    get_document_by_hash,
    get_document_by_id,
    get_stats,
    insert_or_replace_document,
    list_all_documents,
    persist_batch,
)
from nexus_engine.services.storage.export_operations import export_document_markdown, export_knowledge_base
from nexus_engine.services.storage.dedup_operations import analyze_duplicates, apply_refactor
from nexus_engine.services.storage.schema import SCHEMA_SQL, TRAVERSE_QUERY, cosine_similarity

log = logging.getLogger(__name__)


class NexusDatabase:
    """Embedded SQLite database managing relational chunks, graph relations, vectors, and cache."""

    def __init__(self, db_path: str = "data/db/nexus.db") -> None:
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._init_schema()

    def _get_conn(self) -> sqlite3.Connection:
        dir_name = os.path.dirname(os.path.abspath(self.db_path))
        if dir_name:
            os.makedirs(dir_name, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_schema(self) -> None:
        with self._get_conn() as conn:
            conn.executescript(SCHEMA_SQL)
            ensure_schema_compatibility(conn)
            backfill_documents_if_empty(conn)
            conn.commit()

    def reset_database(self) -> None:
        with self._get_conn() as conn:
            clear_all_storage_data(conn)

    # ---- Document Metadata Management ----
    def insert_document(
        self,
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
        with self._get_conn() as conn:
            insert_or_replace_document(
                conn, doc_id, title, format, chunk_count, triple_count, entity_count, duration_ms, status, content_hash
            )

    def list_documents(self) -> list[dict[str, Any]]:
        with self._get_conn() as conn:
            return list_all_documents(conn)

    def get_document_by_hash(self, content_hash: str) -> Optional[dict[str, Any]]:
        with self._get_conn() as conn:
            return get_document_by_hash(conn, content_hash)

    def analyze_knowledge_base(self) -> dict[str, Any]:
        with self._get_conn() as conn:
            return analyze_duplicates(conn)

    def apply_knowledge_refactor(self) -> dict[str, Any]:
        with self._get_conn() as conn:
            return apply_refactor(conn)

    def export_knowledge_base(self) -> dict[str, Any]:
        with self._get_conn() as conn:
            return export_knowledge_base(conn, self.db_path)

    def export_document_markdown(self, doc_id: str) -> Optional[str]:
        with self._get_conn() as conn:
            return export_document_markdown(conn, doc_id)

    def get_document(self, doc_id: str) -> Optional[dict[str, Any]]:
        with self._get_conn() as conn:
            return get_document_by_id(conn, doc_id)

    def delete_document(self, doc_id: str) -> int:
        with self._get_conn() as conn:
            return delete_doc_cascade(conn, doc_id)

    # ---- Chunk Management ----
    def insert_chunk(
        self,
        chunk_id: str,
        doc_id: str,
        level: str,
        title: str,
        content: str,
        path: str,
        parent_id: Optional[str] = None,
        embedding: Optional[Sequence[float]] = None,
    ) -> None:
        with self._get_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO chunks (id, doc_id, level, title, content, path, parent_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (chunk_id, doc_id, level, title, content, path, parent_id),
            )
            if embedding is not None:
                conn.execute(
                    "INSERT OR REPLACE INTO chunk_embeddings (chunk_id, dimension, embedding) VALUES (?, ?, ?)",
                    (chunk_id, len(embedding), json.dumps(list(embedding))),
                )
            conn.commit()

    def get_chunk(self, chunk_id: str) -> Optional[dict[str, Any]]:
        with self._get_conn() as conn:
            row = conn.execute("SELECT * FROM chunks WHERE id = ?", (chunk_id,)).fetchone()
            return dict(row) if row else None

    def list_chunks(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._get_conn() as conn:
            rows = conn.execute("SELECT * FROM chunks ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
            return [dict(r) for r in rows]

    def count_chunks(self) -> int:
        with self._get_conn() as conn:
            row = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()
            return row[0] if row else 0

    # ---- Batch Persistence for Ingestion & Retriever ----
    def persist_graph_and_chunks(
        self,
        chunks: Sequence[Any],
        nodes: dict[str, dict[str, str]],
        edges: dict[tuple[str, str, str], Any],
        embeddings: Optional[dict[str, Sequence[float]]] = None,
    ) -> None:
        with self._get_conn() as conn:
            persist_batch(conn, chunks, nodes, edges, embeddings)

    def get_storage_stats(self) -> dict[str, Any]:
        with self._get_conn() as conn:
            return get_stats(conn, self.db_path)

    def load_all_chunks(self) -> list[dict[str, Any]]:
        with self._get_conn() as conn:
            return fetch_all_chunks_with_embeddings(conn)

    def load_all_graph(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        with self._get_conn() as conn:
            return fetch_all_nodes_and_edges(conn)

    def list_chunks_with_vectors(self, limit: int = 100, doc_id: Optional[str] = None) -> list[dict[str, Any]]:
        with self._get_conn() as conn:
            return fetch_chunks_list(conn, limit=limit, doc_id=doc_id)

    # ---- Vector Search ----
    def vector_search(
        self,
        query_embedding: Sequence[float],
        top_k: int = 5,
        min_score: float = 0.0,
    ) -> list[tuple[dict[str, Any], float]]:
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT c.id, c.doc_id, c.level, c.title, c.content, c.path, c.parent_id, ce.embedding "
                "FROM chunks c JOIN chunk_embeddings ce ON c.id = ce.chunk_id"
            ).fetchall()

            scored: list[tuple[dict[str, Any], float]] = []
            for r in rows:
                vec = json.loads(r["embedding"])
                score = cosine_similarity(query_embedding, vec)
                if score >= min_score:
                    c_dict = {
                        "id": r["id"], "doc_id": r["doc_id"], "level": r["level"],
                        "title": r["title"], "content": r["content"], "path": r["path"],
                        "parent_id": r["parent_id"],
                    }
                    scored.append((c_dict, score))

            scored.sort(key=lambda x: x[1], reverse=True)
            return scored[:top_k]

    # ---- Knowledge Graph Operations ----
    def insert_node(self, node_id: str, label: str, entity_type: str, properties: Optional[dict[str, Any]] = None) -> None:
        with self._get_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO graph_nodes (id, label, entity_type, properties) VALUES (?, ?, ?, ?)",
                (node_id, label, entity_type, json.dumps(properties or {})),
            )
            conn.commit()

    def insert_edge(self, source_id: str, target_id: str, relation: str, weight: float = 1.0) -> None:
        with self._get_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO graph_edges (source_id, target_id, relation, weight) VALUES (?, ?, ?, ?)",
                (source_id, target_id, relation, weight),
            )
            conn.commit()

    def get_node(self, node_id: str) -> Optional[dict[str, Any]]:
        with self._get_conn() as conn:
            row = conn.execute("SELECT * FROM graph_nodes WHERE id = ?", (node_id,)).fetchone()
            if not row:
                return None
            d = dict(row)
            d["properties"] = json.loads(d.get("properties") or "{}")
            return d

    def traverse_subgraph(self, root_node_id: str, max_depth: int = 2) -> list[dict[str, Any]]:
        with self._get_conn() as conn:
            rows = conn.execute(TRAVERSE_QUERY, (root_node_id, max_depth)).fetchall()
            nodes = []
            for r in rows:
                d = dict(r)
                d["properties"] = json.loads(d.get("properties") or "{}")
                nodes.append(d)
            return nodes

    # ---- Persistent Semantic Cache Operations (Tier 2) ----
    def lookup_cache(
        self,
        query_embedding: Sequence[float],
        threshold: float = 0.92,
    ) -> Optional[tuple[dict[str, Any], float]]:
        with self._get_conn() as conn:
            rows = conn.execute("SELECT * FROM semantic_cache").fetchall()
            best_hit: Optional[dict[str, Any]] = None
            best_score = 0.0

            for r in rows:
                vec = json.loads(r["embedding"])
                score = cosine_similarity(query_embedding, vec)
                if score >= threshold and score > best_score:
                    best_score = score
                    best_hit = dict(r)

            if best_hit:
                conn.execute(
                    "UPDATE semantic_cache SET hits = hits + 1, last_accessed = CURRENT_TIMESTAMP WHERE id = ?",
                    (best_hit["id"],),
                )
                conn.commit()
                best_hit["hits"] = best_hit.get("hits", 0) + 1
                best_hit["citations"] = json.loads(best_hit.get("citations") or "[]")
                return best_hit, best_score
            return None

    def insert_cache(
        self,
        cache_id: str,
        query: str,
        answer: str,
        source: str,
        embedding: Sequence[float],
        citations: Optional[Sequence[str]] = None,
    ) -> None:
        with self._get_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO semantic_cache (id, query, answer, source, citations, embedding) VALUES (?, ?, ?, ?, ?, ?)",
                (cache_id, query, answer, source, json.dumps(list(citations or [])), json.dumps(list(embedding))),
            )
            conn.commit()
