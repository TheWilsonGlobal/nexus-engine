"""Hybrid retrieval coordinator: dense vector similarity + graph lookup, fused with RRF (k=60)."""
from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

import numpy as np

from nexus_engine.core.embeddings import EmbedderLike, tokenize
from nexus_engine.services.ingestion.chunker import IngestionChunk
from nexus_engine.services.ingestion.graph_builder import KnowledgeTriple
from nexus_engine.services.retrieval.chunker import Chunk, is_leaf, leaf_text, parse_document
from nexus_engine.services.retrieval.corpus import SAMPLE_DOCUMENTS
from nexus_engine.services.retrieval.graph_store import GraphStore
from nexus_engine.services.retrieval.index_meta import IndexMetaStore
from nexus_engine.services.retrieval.vector_store import VectorStore

log = logging.getLogger(__name__)

RRF_K = 60
# A matching clause also makes its parent (the article it belongs to) a candidate. The parent is scored at this
# fraction of its best matching child's fused score, or at its own score if that is higher, so it follows its
# child in the ranking and is not pulled above it by expansion alone. 0 disables parent expansion.
PARENT_WEIGHT = 0.95
_ARTICLE_TITLE = re.compile(r"^(?:article|điều)\s+(\d+)\b", re.I)


def _article_number(level: str, title: str) -> Optional[str]:
    """Article number recovered from a persisted title, so number lookups survive a restart."""
    m = _ARTICLE_TITLE.match(title or "") if level == "article" else None
    return m.group(1) if m else None


def rrf_fuse(rankings: dict[str, Sequence[str]], k: int = RRF_K) -> dict[str, tuple[float, list[str]]]:
    """Reciprocal Rank Fusion. score(id) = sum over sources of 1 / (k + rank), rank starting at 1."""
    out: dict[str, tuple[float, list[str]]] = {}
    for source, ids in rankings.items():
        for rank, cid in enumerate(ids, start=1):
            s, srcs = out.get(cid, (0.0, []))
            out[cid] = (s + 1.0 / (k + rank), srcs + [source])
    return out


@dataclass
class SearchHit:
    chunk_id: str
    text: str
    path: str
    score: float
    sources: list[str] = field(default_factory=list)
    # Set (on every hit of the query) when the dense leg did not run, e.g. a mixed-dimension index or a
    # failed query embedding: the hit then comes from graph retrieval alone.
    dense_disabled_reason: Optional[str] = None


class HybridRetriever:
    """Dense + graph retrieval.

    Locking: ``_ingest_lock`` serialises writers (and is held across embedding calls, so identical
    concurrent uploads cannot both be added); ``_state_lock`` guards the short in-memory sections that
    mutate or read the retriever/graph dicts. Searches never hold either lock across a network call.
    The vector store itself is lock-free for readers (atomic snapshot swap).
    """

    def __init__(self, nexus_db: Optional[Any] = None, embedder: Optional[EmbedderLike] = None) -> None:
        db_path = getattr(nexus_db, "db_path", None) if nexus_db is not None else None
        meta = IndexMetaStore(db_path) if db_path else None
        self.vector_store = VectorStore(embedder=embedder, meta=meta)
        self.graph = GraphStore()
        self.nexus_db = nexus_db
        self._chunks: dict[str, Any] = {}
        self._leaf_ids: list[str] = []
        self._texts: dict[str, str] = {}
        self._tokens: dict[str, frozenset[str]] = {}
        self._doc_map: dict[str, list[str]] = {}
        self._ingest_lock = threading.RLock()
        self._state_lock = threading.RLock()
        self._last_dense_reason: Optional[str] = None

    @property
    def _matrix(self) -> Optional[np.ndarray]:
        return self.vector_store._matrix

    @property
    def dense_disabled_reason(self) -> Optional[str]:
        return self.vector_store.dense_disabled_reason

    def index_document(self, doc_id: str, raw: str) -> list[Chunk]:
        """Legacy / standard indexer for raw text documents."""
        chunks = parse_document(doc_id, raw, default_title=doc_id)
        by_id = {c.id: c for c in chunks}
        leaf_chunks = [c for c in chunks if is_leaf(c)]
        texts = {c.id: leaf_text(c, by_id) for c in leaf_chunks}
        ingestion_leafs = [
            IngestionChunk(
                id=c.id,
                doc_id=doc_id,
                parent_id=c.parent_id,
                level=c.level,
                title=c.title,
                text=texts[c.id],
                context_header=c.path,
                full_text=texts[c.id],
                tokens=list(frozenset(tokenize(texts[c.id]))),
            )
            for c in leaf_chunks
        ]
        with self._ingest_lock:
            self.vector_store.add_chunks(ingestion_leafs)  # embeds only these, before any state is touched
            with self._state_lock:
                self._chunks.update(by_id)
                self.graph.ingest(chunks)
                for c in leaf_chunks:
                    if c.id not in self._texts:
                        self._leaf_ids.append(c.id)
                        self._doc_map.setdefault(doc_id, []).append(c.id)
                    self._texts[c.id] = texts[c.id]
                    self._tokens[c.id] = frozenset(tokenize(texts[c.id]))
            self._sync_to_db(chunks)
        return chunks

    def _sync_to_db(self, chunks: Sequence[Any]) -> None:
        if not self.nexus_db:
            return
        # Only the vectors of the chunks being persisted: O(new), not O(corpus).
        embeddings = self.vector_store.vectors_for([c.id for c in chunks])
        self.nexus_db.persist_graph_and_chunks(
            chunks=chunks,
            nodes=self.graph.nodes,
            edges=self.graph._edges,
            embeddings=embeddings,
        )

    def ingest_contextual_chunks(
        self,
        chunks: Sequence[IngestionChunk],
        triples: Optional[Sequence[KnowledgeTriple]] = None,
    ) -> None:
        """New Phase 02 ingestion entrypoint for structured IngestionChunk items and knowledge triples."""
        if not chunks:
            return
        doc_id = chunks[0].doc_id
        with self._ingest_lock:
            # Embeds only `chunks` and appends; raises (state untouched) if the vectors do not fit the index.
            self.vector_store.add_chunks(chunks)
            with self._state_lock:
                for c in chunks:
                    self._chunks[c.id] = c
                    self._texts[c.id] = c.text
                    self._tokens[c.id] = frozenset(c.tokens or tokenize(c.full_text))
                    ids = self._doc_map.setdefault(doc_id, [])
                    if c.id not in ids:
                        ids.append(c.id)
                    if c.id not in self._leaf_ids:
                        self._leaf_ids.append(c.id)
                self.graph.ingest(chunks)
                if triples:
                    self.graph.ingest_triples(triples)
            self._sync_to_db(chunks)

    def remove_document(self, doc_id: str) -> None:
        with self._ingest_lock:
            self.vector_store.remove_document(doc_id)  # drops rows by index; no re-embedding
            with self._state_lock:
                cids = set(self._doc_map.pop(doc_id, []))
                for cid in cids:
                    self._chunks.pop(cid, None)
                    self._texts.pop(cid, None)
                    self._tokens.pop(cid, None)
                self._leaf_ids = [cid for cid in self._leaf_ids if cid not in cids]
                self.graph.remove_document(doc_id)
            if self.nexus_db:
                self.nexus_db.delete_document(doc_id)

    def clear(self) -> None:
        with self._ingest_lock:
            self.vector_store.clear()
            with self._state_lock:
                self._chunks.clear()
                self._leaf_ids.clear()
                self._texts.clear()
                self._tokens.clear()
                self._doc_map.clear()
                self.graph.clear()

    def load_from_db(self) -> bool:
        """Rehydrate retriever in-memory caches directly from persistent SQLite storage."""
        if not self.nexus_db:
            return False
        chunk_rows = self.nexus_db.load_all_chunks()
        if not chunk_rows:
            return False

        chunks_new: dict[str, Any] = {}
        leaf_ids: list[str] = []
        texts: dict[str, str] = {}
        tokens: dict[str, frozenset[str]] = {}
        doc_map: dict[str, list[str]] = {}
        ingestion_leafs = []
        embeddings = {}

        for row in chunk_rows:
            cid = row["id"]
            did = row["doc_id"]
            lvl = row["level"]
            title = row["title"]
            text = row["content"]
            path = row["path"]
            pid = row["parent_id"]

            chunks_new[cid] = Chunk(id=cid, level=lvl, title=title, text=text, path=path, parent_id=pid, number=_article_number(lvl, title))
            texts[cid] = text
            tokens[cid] = frozenset(tokenize(text))
            doc_map.setdefault(did, []).append(cid)

            if row.get("embedding"):
                embeddings[cid] = json.loads(row["embedding"])
                leaf_ids.append(cid)
                ingestion_leafs.append(
                    IngestionChunk(
                        id=cid,
                        doc_id=did,
                        parent_id=pid,
                        level=lvl,
                        title=title,
                        text=text,
                        context_header=path,
                        full_text=text,
                        tokens=list(tokens[cid]),
                    )
                )

        nodes, edges = self.nexus_db.load_all_graph()
        with self._ingest_lock:
            # Never drops mismatched rows: an inconsistent index disables dense retrieval with a reason.
            self.vector_store.load_precomputed(ingestion_leafs, embeddings)
            with self._state_lock:
                self._chunks, self._leaf_ids, self._texts, self._tokens, self._doc_map = (
                    chunks_new, leaf_ids, texts, tokens, doc_map,
                )
                self.graph.load_precomputed(nodes, edges)
                self.graph.register_chunks(self._chunks.values(), self._doc_map)
        log.info(
            "HybridRetriever hydrated from nexus.db: %d chunks (%d leaf vectors), %d graph nodes, %d edges",
            len(chunks_new), len(leaf_ids), len(nodes), len(edges)
        )
        reason = self.vector_store.dense_disabled_reason
        if reason:
            log.error("Dense retrieval is disabled after load: %s (graph retrieval still serves queries)", reason)
        return True

    @classmethod
    def with_sample_corpus(cls, nexus_db: Optional[Any] = None, embedder: Optional[EmbedderLike] = None) -> "HybridRetriever":
        r = cls(nexus_db=nexus_db, embedder=embedder)
        if nexus_db and nexus_db.count_chunks() > 0:
            r.load_from_db()
        else:
            for doc_id, raw in SAMPLE_DOCUMENTS.items():
                r.index_document(doc_id, raw)
        return r

    def dense_rank(self, query: str, limit: int = 10) -> list[tuple[str, float]]:
        hits, _reason = self.vector_store.search_ex(query, top_k=limit)
        return [(h.chunk_id, h.score) for h in hits]

    def search(self, query: str, top_k: int = 5) -> list[SearchHit]:
        return self.search_with_info(query, top_k)[0]

    def search_with_info(self, query: str, top_k: int = 5) -> tuple[list[SearchHit], Optional[str]]:
        """(hits, dense_disabled_reason). The query embedding (network) runs before any lock is taken."""
        pool = max(top_k * 3, 10)
        dense_hits, reason = self.vector_store.search_ex(query, top_k=pool)
        if reason != self._last_dense_reason:
            self._last_dense_reason = reason
            if reason:
                log.warning("Hybrid search degraded to graph-only: %s", reason)
        dense = [h.chunk_id for h in dense_hits]
        with self._state_lock:
            graph = [cid for cid, _ in self.graph.rank_chunks(query, pool)]
            fused = rrf_fuse({"dense": dense, "graph": graph})
            self._expand_parents(fused)
            ranked = sorted(fused.items(), key=lambda kv: (-kv[1][0], kv[0]))[:top_k]

            hits: list[SearchHit] = []
            for cid, (score, srcs) in ranked:
                c = self._chunks.get(cid)
                if not c:
                    continue  # removed between the dense leg and here
                path = getattr(c, "path", None) or getattr(c, "context_header", cid)
                text = self._texts.get(cid, getattr(c, "text", ""))
                hits.append(SearchHit(cid, text, path, round(score, 6), srcs, reason))
        return hits, reason

    def _expand_parents(self, fused: dict[str, tuple[float, list[str]]]) -> None:
        """Add each matched chunk's parent to ``fused`` (in place); caller holds ``_state_lock``.

        Only direct parents of chunks that were themselves retrieved, and only parents present in the index.
        """
        if PARENT_WEIGHT <= 0:
            return
        best_child: dict[str, float] = {}
        for cid, (score, _srcs) in fused.items():
            c = self._chunks.get(cid)
            pid = getattr(c, "parent_id", None) if c is not None else None
            if pid and pid != cid and pid in self._chunks:
                best_child[pid] = max(best_child.get(pid, 0.0), score)
        for pid, child_score in best_child.items():
            s, srcs = fused.get(pid, (0.0, []))
            lifted = PARENT_WEIGHT * child_score
            if lifted > s:
                fused[pid] = (lifted, srcs + ["parent"])

    def graph_snapshot(self) -> dict:
        with self._state_lock:
            return self.graph.snapshot()
