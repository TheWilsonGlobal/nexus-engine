"""Vector embedding store: dense similarity ranking over an embedder-pinned, dimension-homogeneous index.

Concurrency model: all index state lives in one immutable ``_State`` snapshot that is replaced atomically.
Readers (``search``, ``get_chunk``...) take the current snapshot without locking, so a concurrent
``remove_document`` can never expose a matrix/leaf-id length mismatch. Writers are serialised by a lock
that is held across the (slow, network) embedding call, which also serialises ingest of identical uploads.

Integrity model: the index is pinned to one embedder identity (provider, model, dim), persisted in the
index database. Vectors of another dimension are refused. If stored vectors disagree with the configured
embedder (mixed dims, changed model), dense retrieval is DISABLED with a reason rather than silently
dropping rows; callers fall back to graph retrieval and surface the reason. Repair is explicit
(``repair_embeddings``).
"""
from __future__ import annotations

import logging
import threading
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

import numpy as np

from nexus_engine.core.embeddings import (
    MOCK_DIM,
    EmbedderIdentity,
    EmbedderLike,
    EmbeddingUnavailable,
    default_embedder,
    tokenize,
)
from nexus_engine.services.ingestion.chunker import IngestionChunk

log = logging.getLogger(__name__)

MIN_DENSE_SCORE = 0.15
MIN_MOCK_DENSE_SCORE = 0.005


class EmbeddingDimensionError(ValueError):
    """Refusal to add vectors whose provider/dimension differs from the index pin."""


@dataclass
class VectorHit:
    chunk_id: str
    score: float


@dataclass(frozen=True)
class _State:
    chunks: Mapping[str, IngestionChunk] = field(default_factory=dict)
    doc_map: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    tokens: Mapping[str, frozenset[str]] = field(default_factory=dict)
    leaf_ids: tuple[str, ...] = ()
    matrix: Optional[np.ndarray] = None
    unembedded: frozenset[str] = frozenset()
    disabled_reason: Optional[str] = None
    # chunk_id -> stored vector dimension; only populated while dense is disabled (for reporting/removal)
    held_dims: Mapping[str, int] = field(default_factory=dict)


class VectorStore:
    def __init__(self, embedder: Optional[EmbedderLike] = None, meta: Optional[Any] = None) -> None:
        self.embedder: EmbedderLike = embedder if embedder is not None else default_embedder()
        self._meta = meta
        self._write_lock = threading.RLock()
        self._state = _State()
        self._pin: Optional[EmbedderIdentity] = meta.load_pin() if meta is not None else None

    # ---- read-only views ------------------------------------------------
    @property
    def _matrix(self) -> Optional[np.ndarray]:
        return self._state.matrix

    @property
    def _leaf_ids(self) -> list[str]:
        return list(self._state.leaf_ids)

    @property
    def total_chunks(self) -> int:
        return len(self._state.chunks)

    @property
    def pin(self) -> Optional[EmbedderIdentity]:
        return self._pin

    @property
    def dense_disabled_reason(self) -> Optional[str]:
        return self._state.disabled_reason

    def get_chunk(self, chunk_id: str) -> Optional[IngestionChunk]:
        return self._state.chunks.get(chunk_id)

    def status(self) -> dict[str, Any]:
        st = self._state
        dims: Counter[int] = Counter(st.held_dims.values())
        if st.matrix is not None:
            dims[int(st.matrix.shape[1])] += int(st.matrix.shape[0])
        return {
            "dense_enabled": st.disabled_reason is None,
            "dense_disabled_reason": st.disabled_reason,
            "embedder": self.embedder.identity.describe(),
            "pinned_to": self._pin.describe() if self._pin else None,
            "vectors": int(st.matrix.shape[0]) if st.matrix is not None else 0,
            "unembedded_chunks": len(st.unembedded),
            "dimension_counts": {str(d): n for d, n in sorted(dims.items())},
        }

    def vectors_for(self, chunk_ids: Sequence[str]) -> dict[str, list[float]]:
        """Stored vectors for the given chunk ids (those that have one), for persistence."""
        st = self._state
        if st.matrix is None:
            return {}
        row_of = {cid: i for i, cid in enumerate(st.leaf_ids)}
        return {cid: st.matrix[row_of[cid]].tolist() for cid in chunk_ids if cid in row_of}

    # ---- writes ----------------------------------------------------------
    def _set_pin(self, identity: EmbedderIdentity) -> None:
        self._pin = identity
        if self._meta is not None:
            self._meta.save_pin(identity)

    def _drop_pin(self) -> None:
        self._pin = None
        if self._meta is not None:
            self._meta.clear_pin()

    def add_chunks(self, chunks: Sequence[IngestionChunk]) -> None:
        """Embed ONLY the given chunks and append them; existing rows are never re-embedded."""
        if not chunks:
            return
        with self._write_lock:
            st = self._state
            incoming = {c.id: c for c in chunks}
            ids = list(incoming)
            vecs: Optional[np.ndarray] = None
            failure: Optional[str] = st.disabled_reason

            if failure is None:
                try:
                    raw = self.embedder.embed_batch([incoming[i].full_text for i in ids], prefix="passage:")
                    vecs = self._validate_batch(raw, len(ids))
                except EmbeddingUnavailable as exc:
                    failure = f"embedding unavailable at ingest: {exc}"
                    log.error("Indexing %d chunks WITHOUT vectors (dense gap, run repair): %s", len(ids), exc)
            else:
                log.warning("Dense retrieval disabled (%s); indexing %d chunks without vectors", failure, len(ids))

            keep = [i for i, cid in enumerate(st.leaf_ids) if cid not in incoming]
            if vecs is not None:
                base = st.matrix[keep] if (st.matrix is not None and keep) else None
                matrix = vecs if base is None else np.vstack([base, vecs])
                leaf_ids = tuple(st.leaf_ids[i] for i in keep) + tuple(ids)
                unembedded = st.unembedded - set(ids)
            else:
                base = st.matrix[keep] if (st.matrix is not None and keep) else None
                matrix = base
                leaf_ids = tuple(st.leaf_ids[i] for i in keep)
                unembedded = st.unembedded | set(ids)

            doc_map = dict(st.doc_map)
            for cid, c in incoming.items():
                doc_map[c.doc_id] = tuple(x for x in doc_map.get(c.doc_id, ()) if x != cid) + (cid,)
            chunks_map = {**st.chunks, **incoming}
            tokens = {**st.tokens, **{cid: frozenset(c.tokens or tokenize(c.full_text)) for cid, c in incoming.items()}}
            held = {k: v for k, v in st.held_dims.items() if k not in incoming}

            if vecs is not None and self._pin is None:
                self._set_pin(self.embedder.identity)
            self._state = _State(
                chunks_map, doc_map, tokens, leaf_ids, matrix, frozenset(unembedded), st.disabled_reason, held
            )
            if vecs is not None:
                log.info("VectorStore +%d vectors: %d total, matrix %s", len(ids), len(leaf_ids), matrix.shape)  # type: ignore[union-attr]

    def _validate_batch(self, raw: Sequence[np.ndarray], expected_n: int) -> np.ndarray:
        """Refuse (loudly) vectors that do not match the index pin / embedder dimension."""
        target = self._pin.dim if self._pin else self.embedder.identity.dim
        if len(raw) != expected_n:
            msg = f"embedder returned {len(raw)} vectors for {expected_n} chunks"
            log.error("Refusing to index: %s", msg)
            raise EmbeddingDimensionError(msg)
        bad = sorted({int(np.asarray(v).shape[0]) for v in raw if np.asarray(v).shape[0] != target})
        if bad:
            msg = (
                f"refusing to add vectors of dimension(s) {bad} to an index pinned to dimension {target} "
                f"({(self._pin or self.embedder.identity).describe()})"
            )
            log.error("EMBEDDING DIMENSION MISMATCH: %s", msg)
            raise EmbeddingDimensionError(msg)
        return np.vstack([np.asarray(v, dtype=np.float32) for v in raw])

    def remove_document(self, doc_id: str) -> int:
        """Drop the document's rows by index. No re-embedding."""
        with self._write_lock:
            st = self._state
            gone = set(st.doc_map.get(doc_id, ()))
            if not gone:
                return 0
            keep = [i for i, cid in enumerate(st.leaf_ids) if cid not in gone]
            matrix = st.matrix[keep] if (st.matrix is not None and keep) else None
            leaf_ids = tuple(st.leaf_ids[i] for i in keep)
            doc_map = {d: ids for d, ids in st.doc_map.items() if d != doc_id}
            self._state = _State(
                {k: v for k, v in st.chunks.items() if k not in gone},
                doc_map,
                {k: v for k, v in st.tokens.items() if k not in gone},
                leaf_ids,
                matrix,
                frozenset(st.unembedded - gone),
                st.disabled_reason,
                {k: v for k, v in st.held_dims.items() if k not in gone},
            )
            if matrix is None and st.disabled_reason is None and self._pin is not None:
                self._drop_pin()  # an index with no vectors has nothing to be pinned to
            return len(gone)

    def clear(self) -> None:
        """Empty the index (the database was emptied too), including its embedder pin."""
        with self._write_lock:
            self._state = _State()
            if self._pin is not None:
                self._drop_pin()

    def load_precomputed(
        self,
        chunks: Sequence[IngestionChunk],
        embeddings: Mapping[str, Sequence[float]],
    ) -> None:
        """Hydrate from persisted vectors. Never silently drops rows: an inconsistent index disables
        dense retrieval with a reason (see ``dense_disabled_reason``) until repaired."""
        with self._write_lock:
            chunk_map: dict[str, IngestionChunk] = {}
            doc_map: dict[str, list[str]] = {}
            tokens: dict[str, frozenset[str]] = {}
            vec_ids: list[str] = []
            vec_list: list[Sequence[float]] = []
            unembedded: set[str] = set()
            for c in chunks:
                chunk_map[c.id] = c
                doc_map.setdefault(c.doc_id, []).append(c.id)
                tokens[c.id] = frozenset(c.tokens or tokenize(c.full_text))
                if c.id in embeddings:
                    vec_ids.append(c.id)
                    vec_list.append(embeddings[c.id])
                else:
                    unembedded.add(c.id)
            frozen_docs = {d: tuple(ids) for d, ids in doc_map.items()}
            ident = self.embedder.identity
            dims = Counter(len(v) for v in vec_list)

            reason: Optional[str] = None
            if len(dims) > 1:
                reason = "mixed_dimensions: stored vectors have dimensions " + ", ".join(
                    f"{d}d x{n}" for d, n in sorted(dims.items())
                ) + "; run repair_embeddings"
            elif dims:
                (stored_dim,) = dims
                if self._pin is not None and self._pin.dim != stored_dim:
                    reason = f"index_pin_mismatch: index pinned to {self._pin.describe()} but stored vectors are {stored_dim}d"
                elif self._pin is not None and self._pin != ident:
                    reason = (
                        f"embedder_changed: index pinned to {self._pin.describe()} but configured embedder is "
                        f"{ident.describe()}; run repair_embeddings"
                    )
                elif stored_dim != ident.dim:
                    reason = f"embedder_dim_mismatch: stored vectors are {stored_dim}d, configured embedder is {ident.describe()}"
            elif self._pin is not None:
                self._drop_pin()  # no vectors: stale pin protects nothing

            if reason is not None:
                log.error("DENSE RETRIEVAL DISABLED: %s", reason)
                self._state = _State(
                    chunk_map, frozen_docs, tokens, (), None, frozenset(unembedded), reason,
                    {cid: len(v) for cid, v in zip(vec_ids, vec_list)},
                )
                return

            matrix = np.array(vec_list, dtype=np.float32) if vec_list else None
            if matrix is not None and self._pin is None:
                self._set_pin(ident)
            self._state = _State(chunk_map, frozen_docs, tokens, tuple(vec_ids), matrix, frozenset(unembedded))
            if matrix is not None:
                log.info("VectorStore rehydrated from DB: %d vectors, matrix %s", len(vec_ids), matrix.shape)

    # ---- search ------------------------------------------------------------
    def search_ex(self, query: str, top_k: int = 10) -> tuple[list[VectorHit], Optional[str]]:
        """(hits, dense_disabled_reason). A reason means the dense leg did not run; hits is then []."""
        st = self._state  # one consistent snapshot for the whole call
        if st.disabled_reason is not None:
            return [], st.disabled_reason
        if st.matrix is None or not st.leaf_ids:
            return [], None
        try:
            q_vec = self.embedder.embed(query, prefix="query:")
        except EmbeddingUnavailable as exc:
            log.warning("Query embedding failed; dense leg skipped (graph retrieval only): %s", exc)
            return [], f"query_embedding_failed: {exc}"
        if q_vec.shape[0] != st.matrix.shape[1]:
            reason = f"query_dim_mismatch: query is {q_vec.shape[0]}d, index is {st.matrix.shape[1]}d"
            log.error("Dense leg skipped: %s", reason)
            return [], reason

        sims = st.matrix @ q_vec
        ids = st.leaf_ids
        order = sorted(range(len(sims)), key=lambda i: (-float(sims[i]), ids[i]))
        qt = frozenset(t for t in tokenize(query) if not t.isdigit())

        # Sparse bag-of-words mock vectors score low by construction; neural embeddings need a real floor.
        is_mock = self.embedder.identity.provider == "mock" or st.matrix.shape[1] == MOCK_DIM
        min_score = MIN_MOCK_DENSE_SCORE if is_mock else MIN_DENSE_SCORE

        hits: list[VectorHit] = []
        for i in order:
            cid = ids[i]
            score = float(sims[i])
            if score < min_score:
                break
            if not qt or (qt & {t for t in st.tokens.get(cid, ()) if not t.isdigit()}):
                hits.append(VectorHit(chunk_id=cid, score=score))
            if len(hits) >= top_k:
                break
        return hits, None

    def search(self, query: str, top_k: int = 10) -> list[VectorHit]:
        return self.search_ex(query, top_k)[0]
