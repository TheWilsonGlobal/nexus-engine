"""SQLite database schema definitions and vector math helpers for NexusDatabase."""
from __future__ import annotations

import math
from typing import Sequence

SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- 0. Indexed Documents Registry
CREATE TABLE IF NOT EXISTS documents (
    doc_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    format TEXT NOT NULL DEFAULT 'text',
    chunk_count INTEGER NOT NULL DEFAULT 0,
    triple_count INTEGER NOT NULL DEFAULT 0,
    entity_count INTEGER NOT NULL DEFAULT 0,
    duration_ms REAL DEFAULT 0.0,
    content_hash TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'indexed',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(status);

-- 1. Document Chunks & Metadata
CREATE TABLE IF NOT EXISTS chunks (
    id TEXT PRIMARY KEY,
    doc_id TEXT NOT NULL,
    level TEXT NOT NULL,         -- 'document' | 'chapter' | 'article' | 'clause'
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    path TEXT NOT NULL,
    parent_id TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_chunks_parent ON chunks(parent_id);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id);

-- 2. Chunk Embeddings (dense float vectors)
CREATE TABLE IF NOT EXISTS chunk_embeddings (
    chunk_id TEXT PRIMARY KEY,
    dimension INTEGER NOT NULL,
    embedding JSON NOT NULL,
    FOREIGN KEY (chunk_id) REFERENCES chunks(id) ON DELETE CASCADE
);

-- 3. Knowledge Graph Nodes & Edges
CREATE TABLE IF NOT EXISTS graph_nodes (
    id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    entity_type TEXT NOT NULL,    -- 'document' | 'article' | 'entity' | 'concept'
    properties JSON NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS graph_edges (
    source_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    relation TEXT NOT NULL,       -- 'contains' | 'cites' | 'mentions' | 'defines'
    weight REAL DEFAULT 1.0,
    PRIMARY KEY (source_id, target_id, relation),
    FOREIGN KEY (source_id) REFERENCES graph_nodes(id) ON DELETE CASCADE,
    FOREIGN KEY (target_id) REFERENCES graph_nodes(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_edge_target ON graph_edges(target_id, relation);

-- 4. Persistent Semantic Cache
CREATE TABLE IF NOT EXISTS semantic_cache (
    id TEXT PRIMARY KEY,
    query TEXT NOT NULL,
    answer TEXT NOT NULL,
    source TEXT NOT NULL,         -- 'verified' | 'audit_precedent'
    citations JSON NOT NULL DEFAULT '[]',
    hits INTEGER DEFAULT 0,
    embedding JSON NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_accessed TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_cache_query ON semantic_cache(query);
"""


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


TRAVERSE_QUERY = """
WITH RECURSIVE graph_subgraph(node_id, depth, path) AS (
    SELECT id, 0, id FROM graph_nodes WHERE id = ?
    UNION ALL
    SELECT e.target_id, s.depth + 1, s.path || '->' || e.target_id
    FROM graph_edges e
    JOIN graph_subgraph s ON e.source_id = s.node_id
    WHERE s.depth < ? AND s.path NOT LIKE '%' || e.target_id || '%'
)
SELECT DISTINCT n.id, n.label, n.entity_type, n.properties, g.depth
FROM graph_nodes n
JOIN graph_subgraph g ON n.id = g.node_id
ORDER BY g.depth ASC;
"""
