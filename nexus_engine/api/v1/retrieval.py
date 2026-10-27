from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from nexus_engine.api.deps import get_container, rate_limit, require_roles
from nexus_engine.core.container import Container
from nexus_engine.schemas.retrieval import GraphResponse, SearchRequest, SearchResponse
from nexus_engine.services.security.validator import validate_identifier

router = APIRouter(prefix="/retrieval")

MAX_CHUNKS_LIMIT = 500


@router.post(
    "/search",
    response_model=SearchResponse,
    dependencies=[Depends(rate_limit("general")), Depends(require_roles("admin", "operator", "auditor", "public_viewer"))],
)
def search(body: SearchRequest, c: Container = Depends(get_container)) -> dict:
    hits = c.retriever.search(body.query, body.top_k)
    return {
        "results": [
            {"chunk_id": h.chunk_id, "text": h.text, "path": h.path, "score": h.score, "sources": h.sources}
            for h in hits
        ]
    }


@router.get(
    "/graph",
    response_model=GraphResponse,
    dependencies=[Depends(rate_limit("general")), Depends(require_roles("admin", "operator", "auditor", "public_viewer"))],
)
def graph(c: Container = Depends(get_container)) -> dict:
    return c.retriever.graph_snapshot()


@router.get(
    "/storage-stats",
    dependencies=[Depends(rate_limit("general")), Depends(require_roles("admin", "operator", "auditor", "public_viewer"))],
)
def storage_stats(c: Container = Depends(get_container)) -> dict:
    """Return live persistent SQLite storage counts directly from nexus.db with active embedding info."""
    stats = c.nexus_db.get_storage_stats()
    matrix = c.retriever.vector_store._matrix
    dim = int(matrix.shape[1]) if matrix is not None else 0
    is_fallback = dim == 4096 or dim == 0
    stats["vector_dimension"] = dim
    stats["is_fallback"] = is_fallback
    stats["active_embedding_model"] = (
        "deterministic-bow (fallback)" if is_fallback else c.settings.embedding_model
    )
    return stats


@router.get(
    "/chunks",
    dependencies=[Depends(rate_limit("general")), Depends(require_roles("admin", "operator", "auditor", "public_viewer"))],
)
def list_chunks(
    limit: int = Query(default=100, ge=1, le=MAX_CHUNKS_LIMIT),
    doc_id: str | None = None,
    c: Container = Depends(get_container),
) -> dict:
    """Return stored chunks with vector status from nexus.db."""
    if doc_id is not None:
        doc_id = validate_identifier(doc_id, "doc_id")
    chunks = c.nexus_db.list_chunks_with_vectors(limit=limit, doc_id=doc_id)
    return {"chunks": chunks, "total": len(chunks)}


