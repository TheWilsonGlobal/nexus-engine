"""FastAPI endpoints for document ingestion, sample pack loading, and document management."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response

from nexus_engine.api.audit import audit_event
from nexus_engine.api.deps import get_container, get_current_user, rate_limit, require_roles
from nexus_engine.api.errors import internal_error
from nexus_engine.core.container import Container
from nexus_engine.schemas.auth import AuthenticatedUser
from nexus_engine.schemas.ingestion import (
    DocumentListResponse,
    DocumentMetaItem,
    IngestionResponse,
    KnowledgeExportResponse,
    RefactorAnalysisResponse,
    RefactorApplyResponse,
    SamplePackResponse,
)
from nexus_engine.services.ingestion.parser_models import DocumentParseError
from nexus_engine.services.security.validator import sanitize_upload_filename, validate_identifier

log = logging.getLogger(__name__)

router = APIRouter(prefix="/ingestion", tags=["ingestion"])


@router.post(
    "/upload",
    response_model=IngestionResponse,
    dependencies=[Depends(rate_limit("ingestion")), Depends(require_roles("admin", "operator"))],
)
async def upload_document(
    request: Request,
    file: UploadFile = File(...),
    doc_id: Optional[str] = Form(None),
    format_hint: Optional[str] = Form(None),
    c: Container = Depends(get_container),
    user: AuthenticatedUser = Depends(get_current_user),
) -> IngestionResponse:
    """Upload a raw document (.json, .md, .txt, .pdf) to parse, chunk, and index into knowledge base."""
    filename = sanitize_upload_filename(file.filename or "uploaded_document.txt")
    if doc_id is not None:
        doc_id = validate_identifier(doc_id, "doc_id")
    try:
        raw_bytes = await file.read()
        if not raw_bytes:
            raise HTTPException(status_code=400, detail="Uploaded file is empty.")
        max_bytes = c.settings.max_payload_size_mb * 1024 * 1024
        if len(raw_bytes) > max_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"File size exceeds maximum {c.settings.max_payload_size_mb}MB limit.",
            )

        # Parsing, remote embedding (network + sleeps) and SQLite are blocking: keep them off the event loop
        # so /health, SSE streams and other requests stay responsive during a slow ingest.
        summary = await run_in_threadpool(
            c.pipeline.ingest_document,
            raw_bytes=raw_bytes,
            filename=filename,
            format_hint=format_hint,
            doc_id=doc_id,
        )
        result = IngestionResponse(**summary.to_dict())
        audit_event(request, user, "ingestion.upload", f"file={filename} doc_id={result.doc_id}")
        return result
    except HTTPException:
        raise
    except DocumentParseError as exc:
        log.warning("Rejected unparseable upload %s: %s", filename, exc)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise internal_error(log, "Ingestion", exc) from exc


@router.post(
    "/sample-pack",
    response_model=SamplePackResponse,
    dependencies=[Depends(rate_limit("ingestion")), Depends(require_roles("admin", "operator"))],
)
def ingest_sample_pack(c: Container = Depends(get_container)) -> SamplePackResponse:
    """Trigger batch ingestion of the 3 pre-packaged raw sample datasets (Legal, Corporate SOP, Export Controls)."""
    try:
        summaries = c.pipeline.ingest_sample_pack()
        docs = [IngestionResponse(**s.to_dict()) for s in summaries]
        total_chunks = sum(d.chunk_count for d in docs)
        total_triples = sum(d.triple_count for d in docs)
        total_dur = sum(d.duration_ms for d in docs)
        return SamplePackResponse(
            ingested_documents=docs,
            total_chunks=total_chunks,
            total_triples=total_triples,
            total_duration_ms=round(total_dur, 2),
        )
    except Exception as exc:
        raise internal_error(log, "Sample pack ingestion", exc) from exc


@router.post(
    "/reset",
    response_model=SamplePackResponse,
    dependencies=[Depends(rate_limit("ingestion")), Depends(require_roles("admin"))],
)
def reset_knowledge(
    request: Request,
    c: Container = Depends(get_container),
    user: AuthenticatedUser = Depends(get_current_user),
) -> SamplePackResponse:
    """Clear all data from SQLite (nexus.db) and in-memory caches, then re-ingest clean sample pack."""
    try:
        summaries = c.pipeline.reset_knowledge_base()
        c.memory.cache.clear()
        audit_event(request, user, "ingestion.reset", "knowledge base wiped and sample pack re-ingested")
        docs = [IngestionResponse(**s.to_dict()) for s in summaries]
        total_chunks = sum(d.chunk_count for d in docs)
        total_triples = sum(d.triple_count for d in docs)
        total_dur = sum(d.duration_ms for d in docs)
        return SamplePackResponse(
            ingested_documents=docs,
            total_chunks=total_chunks,
            total_triples=total_triples,
            total_duration_ms=round(total_dur, 2),
        )
    except Exception as exc:
        audit_event(request, user, "ingestion.reset", "knowledge base reset", outcome="failed")
        raise internal_error(log, "Reset knowledge base", exc) from exc


@router.post(
    "/refactor/analyze",
    response_model=RefactorAnalysisResponse,
    dependencies=[Depends(rate_limit("ingestion")), Depends(require_roles("admin", "operator"))],
)
def analyze_refactor(c: Container = Depends(get_container)) -> RefactorAnalysisResponse:
    """Analyze SQLite nexus.db for duplicate documents, chunks, vectors, and graph nodes (dry-run)."""
    try:
        report = c.nexus_db.analyze_knowledge_base()
        return RefactorAnalysisResponse(**report)
    except Exception as exc:
        raise internal_error(log, "Analysis", exc) from exc


@router.post(
    "/refactor/apply",
    response_model=RefactorApplyResponse,
    dependencies=[Depends(rate_limit("ingestion")), Depends(require_roles("admin"))],
)
def apply_refactor(
    request: Request,
    c: Container = Depends(get_container),
    user: AuthenticatedUser = Depends(get_current_user),
) -> RefactorApplyResponse:
    """Execute deduplication and consolidation on nexus.db, and rehydrate retriever caches."""
    try:
        result = c.nexus_db.apply_knowledge_refactor()
        c.retriever.load_from_db()
        c.pipeline._rehydrate_registry()
        c.memory.cache.clear()
        audit_event(request, user, "ingestion.refactor_apply", "knowledge base deduplicated and consolidated")
        return RefactorApplyResponse(**result)
    except Exception as exc:
        audit_event(request, user, "ingestion.refactor_apply", "knowledge base refactor", outcome="failed")
        raise internal_error(log, "Refactoring", exc) from exc


def _attachment(filename: str) -> dict[str, str]:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", filename).strip("._") or "download"
    return {"Content-Disposition": f'attachment; filename="{safe}"'}


@router.get(
    "/documents/export",
    response_model=KnowledgeExportResponse,
    dependencies=[Depends(rate_limit("general")), Depends(require_roles("admin", "operator", "auditor"))],
)
def export_knowledge_base(c: Container = Depends(get_container)) -> JSONResponse:
    """Download every indexed document, chunk, graph node and edge as one JSON file (no embeddings)."""
    snapshot = KnowledgeExportResponse(**c.nexus_db.export_knowledge_base())
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return JSONResponse(snapshot.model_dump(), headers=_attachment(f"knowledge-base-{stamp}.json"))


@router.get(
    "/documents/{doc_id}/export",
    dependencies=[Depends(rate_limit("general")), Depends(require_roles("admin", "operator", "auditor"))],
)
def export_document(doc_id: str, c: Container = Depends(get_container)) -> Response:
    """Download one indexed document rebuilt as Markdown from its chunks."""
    validate_identifier(doc_id, "doc_id")
    markdown = c.nexus_db.export_document_markdown(doc_id)
    if markdown is None:
        raise HTTPException(status_code=404, detail=f"Document '{doc_id}' not found in index.")
    return Response(
        markdown, media_type="text/markdown; charset=utf-8", headers=_attachment(f"{doc_id}.md")
    )


@router.get(
    "/documents",
    response_model=DocumentListResponse,
    dependencies=[Depends(rate_limit("general")), Depends(require_roles("admin", "operator", "auditor", "public_viewer"))],
)
def list_documents(c: Container = Depends(get_container)) -> DocumentListResponse:
    """List all currently indexed documents with summary statistics."""
    docs = [DocumentMetaItem(**item) for item in c.pipeline.list_documents()]
    total_chunks = sum(d.chunk_count for d in docs)
    total_triples = sum(d.triple_count for d in docs)
    return DocumentListResponse(
        documents=docs,
        total_documents=len(docs),
        total_chunks=total_chunks,
        total_triples=total_triples,
    )


@router.delete(
    "/documents/{doc_id}",
    dependencies=[Depends(rate_limit("ingestion")), Depends(require_roles("admin"))],
)
def delete_document(
    doc_id: str,
    request: Request,
    c: Container = Depends(get_container),
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, str]:
    """Delete an indexed document and all its associated vector chunks and graph nodes."""
    validate_identifier(doc_id, "doc_id")
    success = c.pipeline.delete_document(doc_id)
    if success:
        # Cached "verified" answers may cite the deleted document; drop them all.
        c.memory.cache.clear()
        audit_event(request, user, "ingestion.delete_document", f"doc_id={doc_id}")
    if not success:
        raise HTTPException(status_code=404, detail=f"Document '{doc_id}' not found in index.")
    return {"status": "deleted", "doc_id": doc_id}
