"""Read-only export of the knowledge base: whole-KB JSON snapshot and per-document Markdown.

Original upload bytes are not stored, so exports are rebuilt from the indexed chunks and graph.
Embeddings are deliberately excluded (large, and reproducible from the chunk text).
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

from nexus_engine.services.storage.operations import get_stats, list_all_documents

EXPORT_VERSION = 1
_CHUNK_COLUMNS = "id, doc_id, level, title, content, path, parent_id"
_MAX_HEADING = 6


def _graph_nodes(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT id, label, entity_type, properties FROM graph_nodes ORDER BY id").fetchall()
    nodes: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        raw = d.get("properties")
        if isinstance(raw, str):
            try:
                d["properties"] = json.loads(raw)
            except ValueError:
                pass
        nodes.append(d)
    return nodes


def export_knowledge_base(conn: sqlite3.Connection, db_path: str = "") -> dict[str, Any]:
    """Snapshot of every document, chunk, graph node and edge (no embeddings)."""
    chunks = conn.execute(f"SELECT {_CHUNK_COLUMNS} FROM chunks ORDER BY doc_id, rowid").fetchall()
    edges = conn.execute(
        "SELECT source_id, target_id, relation, weight FROM graph_edges ORDER BY source_id, target_id, relation"
    ).fetchall()
    nodes = _graph_nodes(conn)
    return {
        "version": EXPORT_VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "stats": get_stats(conn, db_path),
        "documents": list_all_documents(conn),
        "chunks": [dict(r) for r in chunks],
        "graph": {"nodes": nodes, "edges": [dict(r) for r in edges]},
    }


def _natural_key(chunk_id: str) -> list[Any]:
    """Sort ids such as 'doc:p1:c2:a10' in document order (a2 before a10)."""
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", chunk_id)]


def _breadcrumb(path: str, doc_title: str) -> list[str]:
    parts = [p.strip() for p in (path or "").split(" > ") if p.strip()]
    if parts and parts[0].startswith("Document:"):
        parts = parts[1:]
    return [p for p in parts if p != doc_title]


def export_document_markdown(conn: sqlite3.Connection, doc_id: str) -> Optional[str]:
    """Rebuild one document as Markdown (None if the document is unknown).

    Parent chunks repeat the text of their children, so only leaf chunks are written. They are
    emitted in document order under their ancestor headings.
    """
    doc = conn.execute("SELECT doc_id, title FROM documents WHERE doc_id = ?", (doc_id,)).fetchone()
    if doc is None:
        return None
    rows = [dict(r) for r in conn.execute(f"SELECT {_CHUNK_COLUMNS} FROM chunks WHERE doc_id = ?", (doc_id,))]
    parent_ids = {r["parent_id"] for r in rows if r["parent_id"]}
    leaves = sorted((r for r in rows if r["id"] not in parent_ids), key=lambda r: _natural_key(r["id"]))

    lines = [f"# {doc['title']}", ""]
    open_path: list[str] = []
    for r in leaves:
        crumb = _breadcrumb(r["path"], doc["title"]) or [r["title"]]
        for depth, heading in enumerate(crumb, start=2):
            if open_path[: depth - 1] == crumb[: depth - 1]:
                continue  # this ancestor heading is already open
            lines.extend([f"{'#' * min(depth, _MAX_HEADING)} {heading}", ""])
        open_path = crumb
        body = (r["content"] or "").strip()
        if body:
            lines.extend([body, ""])
    return "\n".join(lines).rstrip() + "\n"
