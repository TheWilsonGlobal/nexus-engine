"""Deduplication and refactoring operations for SQLite nexus.db."""
from __future__ import annotations

import re
import sqlite3
import unicodedata
from collections import defaultdict
from typing import Any

from nexus_engine.services.storage.operations import doc_scope_clause

# Only graph nodes minted by the entity extractor may ever be merged. Chunk/structure nodes
# (``doc/chap/art1/cl1`` ...) legitimately share labels such as "Clause 1" across documents.
ENTITY_NODE_PREFIX = "entity:"


def _is_entity_node(node_id: str) -> bool:
    return node_id.startswith(ENTITY_NODE_PREFIX)


def _redundant_chunk_ids(conn: sqlite3.Connection, skip_doc_ids: set[str]) -> set[str]:
    """Chunks that are exact repeats (same doc_id + content) and safe to drop.

    A chunk is never droppable if it takes part in the hierarchy: it has a ``parent_id``
    (child) or is referenced by another chunk's ``parent_id`` (parent). The earliest row of
    each group is always kept.
    """
    rows = conn.execute(
        "SELECT c.id, c.doc_id, c.content FROM chunks c "
        "WHERE c.parent_id IS NULL "
        "AND c.id NOT IN (SELECT parent_id FROM chunks WHERE parent_id IS NOT NULL) "
        "AND c.rowid NOT IN (SELECT MIN(rowid) FROM chunks GROUP BY doc_id, content) "
        "ORDER BY c.rowid"
    ).fetchall()
    return {r["id"] for r in rows if r["doc_id"] not in skip_doc_ids}


def _canonical_key(text: str) -> str:
    norm = unicodedata.normalize("NFC", text or "").lower()
    return re.sub(r"[^a-z0-9]+", "", norm)


def analyze_duplicates(conn: sqlite3.Connection) -> dict[str, Any]:
    """Inspect nexus.db for duplicate documents, chunks, vectors, and graph elements (dry-run)."""
    doc_rows = conn.execute("SELECT * FROM documents ORDER BY created_at ASC").fetchall()
    docs = [dict(r) for r in doc_rows]

    # Group documents by content_hash (if set) or normalized title
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for d in docs:
        chash = (d.get("content_hash") or "").strip()
        key = f"hash:{chash}" if chash else f"title:{_canonical_key(d.get('title', ''))}"
        groups[key].append(d)

    duplicate_groups = []
    removable_doc_ids: set[str] = set()
    total_dup_docs = 0

    for _, group_docs in groups.items():
        if len(group_docs) <= 1:
            continue
        primary = group_docs[0]
        items = []
        for i, d in enumerate(group_docs):
            is_dup = i > 0
            if is_dup:
                removable_doc_ids.add(d["doc_id"])
                total_dup_docs += 1
            items.append({
                "doc_id": d["doc_id"],
                "title": d["title"],
                "chunk_count": d.get("chunk_count", 0),
                "created_at": str(d.get("created_at", "")),
                "action": "remove" if is_dup else "keep",
            })
        duplicate_groups.append({
            "canonical_title": primary["title"],
            "primary_doc_id": primary["doc_id"],
            "duplicates": items,
        })

    # Chunk & Vector inspection
    all_chunks = conn.execute("SELECT id, doc_id, path, content FROM chunks").fetchall()
    removable_chunk_ids: set[str] = set()

    # Chunks belonging to removable duplicate documents
    for c in all_chunks:
        if c["doc_id"] in removable_doc_ids:
            removable_chunk_ids.add(c["id"])

    # Duplicate chunks within kept documents (identical text, never hierarchy members)
    removable_chunk_ids |= _redundant_chunk_ids(conn, removable_doc_ids)

    # Removable vectors
    duplicate_vectors = []
    if removable_chunk_ids:
        placeholders = ",".join("?" for _ in removable_chunk_ids)
        vec_rows = conn.execute(
            f"SELECT chunk_id, dimension FROM chunk_embeddings WHERE chunk_id IN ({placeholders})",
            list(removable_chunk_ids),
        ).fetchall()
        duplicate_vectors = [{"chunk_id": r["chunk_id"], "dimension": r["dimension"]} for r in vec_rows]
    removable_vec_count = len(duplicate_vectors)

    duplicate_chunks = [
        {"chunk_id": c["id"], "doc_id": c["doc_id"], "path": c["path"] or "", "snippet": (c["content"] or "")[:100].strip()}
        for c in all_chunks if c["id"] in removable_chunk_ids
    ]

    # Graph node & edge inspection
    nodes = conn.execute("SELECT id, label, entity_type FROM graph_nodes").fetchall()
    node_groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    mergeable_nodes_count = 0

    for n in nodes:
        nid = n["id"]
        if not _is_entity_node(nid) and nid.split(":")[0].split("/")[0] not in removable_doc_ids:
            continue
        doc_prefix = nid.split(":")[0] if ":" in nid else nid.split("/")[0]
        if doc_prefix in removable_doc_ids:
            mergeable_nodes_count += 1
            continue
        key = (n["entity_type"], _canonical_key(n["label"]))
        node_groups[key].append(dict(n))

    mergeable_nodes = []
    for k, n_list in node_groups.items():
        if len(n_list) > 1:
            mergeable_nodes_count += (len(n_list) - 1)
            mergeable_nodes.append({
                "label": n_list[0]["label"],
                "entity_type": k[0],
                "canonical_id": n_list[0]["id"],
                "duplicate_ids": [x["id"] for x in n_list[1:]],
            })

    # Redundant parallel edges
    edge_rows = conn.execute(
        "SELECT source_id, target_id, relation, COUNT(*) as cnt FROM graph_edges GROUP BY source_id, target_id, relation HAVING cnt > 1"
    ).fetchall()
    redundant_edges = [
        {"source_id": r["source_id"], "target_id": r["target_id"], "relation": r["relation"], "count": r["cnt"]}
        for r in edge_rows
    ]
    redundant_edges_count = sum(r["count"] - 1 for r in redundant_edges)

    # Current totals
    total_docs = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    total_chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    total_vecs = conn.execute("SELECT COUNT(*) FROM chunk_embeddings").fetchone()[0]
    total_nodes = len(nodes)
    total_edges = conn.execute("SELECT COUNT(*) FROM graph_edges").fetchone()[0]

    current_stats = {
        "documents": total_docs,
        "chunks": total_chunks,
        "embeddings": total_vecs,
        "nodes": total_nodes,
        "edges": total_edges,
    }

    projected_stats = {
        "documents": max(0, total_docs - total_dup_docs),
        "chunks": max(0, total_chunks - len(removable_chunk_ids)),
        "embeddings": max(0, total_vecs - removable_vec_count),
        "nodes": max(0, total_nodes - mergeable_nodes_count),
        "edges": max(0, total_edges - redundant_edges_count),
    }

    has_duplicates = (total_dup_docs > 0 or len(removable_chunk_ids) > 0 or mergeable_nodes_count > 0 or redundant_edges_count > 0)

    return {
        "has_duplicates": has_duplicates,
        "duplicate_documents_count": total_dup_docs,
        "duplicate_chunks_count": len(removable_chunk_ids),
        "duplicate_vectors_count": removable_vec_count,
        "mergeable_nodes_count": mergeable_nodes_count,
        "redundant_edges_count": redundant_edges_count,
        "duplicate_groups": duplicate_groups,
        "duplicate_chunks": duplicate_chunks,
        "duplicate_vectors": duplicate_vectors,
        "mergeable_nodes": mergeable_nodes,
        "redundant_edges": redundant_edges,
        "current_stats": current_stats,
        "projected_stats": projected_stats,
    }


def apply_refactor(conn: sqlite3.Connection) -> dict[str, Any]:
    """Execute transactional cleanup and consolidation of duplicates in nexus.db."""
    analysis = analyze_duplicates(conn)
    if not analysis["has_duplicates"]:
        return {
            "status": "clean",
            "removed_docs": 0,
            "removed_chunks": 0,
            "removed_vectors": 0,
            "merged_nodes": 0,
            "removed_edges": 0,
            "final_stats": analysis["current_stats"],
        }

    # 1. Prune duplicate documents
    removable_doc_ids: list[str] = []
    for g in analysis["duplicate_groups"]:
        for d in g["duplicates"]:
            if d["action"] == "remove":
                removable_doc_ids.append(d["doc_id"])

    removed_docs = 0
    removed_chunks = 0
    removed_vectors = 0

    if removable_doc_ids:
        ph = ",".join("?" for _ in removable_doc_ids)
        # Count chunks to remove
        c_res = conn.execute(f"SELECT COUNT(*) FROM chunks WHERE doc_id IN ({ph})", removable_doc_ids).fetchone()
        removed_chunks += c_res[0] if c_res else 0

        # Delete embeddings
        v_res = conn.execute(
            f"DELETE FROM chunk_embeddings WHERE chunk_id IN (SELECT id FROM chunks WHERE doc_id IN ({ph}))",
            removable_doc_ids,
        )
        removed_vectors += v_res.rowcount

        # Delete chunks
        conn.execute(f"DELETE FROM chunks WHERE doc_id IN ({ph})", removable_doc_ids)

        # Delete documents
        d_res = conn.execute(f"DELETE FROM documents WHERE doc_id IN ({ph})", removable_doc_ids)
        removed_docs = d_res.rowcount

    # 2. Prune exact-duplicate leaf chunks (never parents, never children)
    extra_chunk_ids = sorted(_redundant_chunk_ids(conn, set()))
    if extra_chunk_ids:
        ph_c = ",".join("?" for _ in extra_chunk_ids)
        v_res = conn.execute(f"DELETE FROM chunk_embeddings WHERE chunk_id IN ({ph_c})", extra_chunk_ids)
        removed_vectors += v_res.rowcount
        c_res = conn.execute(f"DELETE FROM chunks WHERE id IN ({ph_c})", extra_chunk_ids)
        removed_chunks += c_res.rowcount

    # 3. Clean orphan chunk embeddings
    orphan_v = conn.execute("DELETE FROM chunk_embeddings WHERE chunk_id NOT IN (SELECT id FROM chunks)")
    removed_vectors += orphan_v.rowcount

    # 4. Graph consolidation & node merging
    all_nodes = [
        dict(r) for r in conn.execute("SELECT id, label, entity_type FROM graph_nodes ORDER BY id").fetchall()
    ]
    node_groups: dict[tuple[str, str], list[str]] = defaultdict(list)
    for n in all_nodes:
        if not _is_entity_node(n["id"]):
            continue
        key = (n["entity_type"], _canonical_key(n["label"]))
        node_groups[key].append(n["id"])

    merged_nodes = 0
    for _, ids in node_groups.items():
        if len(ids) <= 1:
            continue
        canonical_id = ids[0]
        duplicate_ids = ids[1:]
        merged_nodes += len(duplicate_ids)
        for dup_id in duplicate_ids:
            # Rewire edges
            conn.execute("UPDATE OR IGNORE graph_edges SET source_id = ? WHERE source_id = ?", (canonical_id, dup_id))
            conn.execute("UPDATE OR IGNORE graph_edges SET target_id = ? WHERE target_id = ?", (canonical_id, dup_id))
            # Delete remaining duplicate edges & self loops
            conn.execute("DELETE FROM graph_edges WHERE source_id = ? OR target_id = ?", (dup_id, dup_id))
            conn.execute("DELETE FROM graph_nodes WHERE id = ?", (dup_id,))

    # Remove self loops and orphan graph nodes belonging to removed documents
    conn.execute("DELETE FROM graph_edges WHERE source_id = target_id")
    if removable_doc_ids:
        for did in removable_doc_ids:
            node_sql, node_params = doc_scope_clause("id", did)
            src_sql, src_params = doc_scope_clause("source_id", did)
            tgt_sql, tgt_params = doc_scope_clause("target_id", did)
            conn.execute(f"DELETE FROM graph_edges WHERE {src_sql} OR {tgt_sql}", src_params + tgt_params)
            conn.execute(f"DELETE FROM graph_nodes WHERE {node_sql}", node_params)

    conn.commit()

    final_stats = {
        "documents": conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0],
        "chunks": conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0],
        "embeddings": conn.execute("SELECT COUNT(*) FROM chunk_embeddings").fetchone()[0],
        "nodes": conn.execute("SELECT COUNT(*) FROM graph_nodes").fetchone()[0],
        "edges": conn.execute("SELECT COUNT(*) FROM graph_edges").fetchone()[0],
    }

    return {
        "status": "refactored",
        "removed_docs": removed_docs,
        "removed_chunks": removed_chunks,
        "removed_vectors": removed_vectors,
        "merged_nodes": merged_nodes,
        "removed_edges": max(0, analysis["current_stats"]["edges"] - final_stats["edges"]),
        "final_stats": final_stats,
    }
