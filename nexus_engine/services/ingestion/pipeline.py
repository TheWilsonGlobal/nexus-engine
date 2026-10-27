"""End-to-end ingestion pipeline runner: raw document loading, parsing, chunking, and dual-indexing."""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from nexus_engine.services.ingestion.chunker import ContextualChunker, IngestionChunk
from nexus_engine.services.ingestion.graph_builder import GraphBuilder, KnowledgeTriple
from nexus_engine.services.ingestion.parser import HierarchicalParser, ParsedDocument

if TYPE_CHECKING:
    from nexus_engine.services.retrieval.hybrid_retriever import HybridRetriever

log = logging.getLogger(__name__)


@dataclass
class IngestionSummary:
    doc_id: str
    title: str
    format: str
    chunk_count: int
    triple_count: int
    entity_count: int
    duration_ms: float
    status: str = "indexed"
    content_hash: str = ""
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class IngestionPipeline:
    def __init__(self, retriever: HybridRetriever, nexus_db: Optional[Any] = None) -> None:
        self.retriever = retriever
        self.nexus_db = nexus_db
        self.parser = HierarchicalParser()
        self.chunker = ContextualChunker()
        self.graph_builder = GraphBuilder()
        self._registry: dict[str, IngestionSummary] = {}
        # Serialises ingest/delete/reset: the duplicate-hash check and the add must be one atomic step,
        # otherwise two identical concurrent uploads both pass the check and are both indexed.
        self._ingest_lock = threading.RLock()
        if self.nexus_db:
            self._rehydrate_registry()

    def _rehydrate_registry(self) -> None:
        if not self.nexus_db:
            return
        # Rebuild from the database (source of truth) and swap atomically: entries for documents
        # that no longer exist (refactor/delete/reset) must not linger and shadow new uploads
        # as "skipped_duplicate".
        fresh: dict[str, IngestionSummary] = {}
        docs = self.nexus_db.list_documents()
        for d in docs:
            fresh[d["doc_id"]] = IngestionSummary(
                doc_id=d["doc_id"],
                title=d["title"],
                format=d.get("format", "text"),
                chunk_count=d.get("chunk_count", 0),
                triple_count=d.get("triple_count", 0),
                entity_count=d.get("entity_count", 0),
                duration_ms=d.get("duration_ms", 0.0),
                status=d.get("status", "indexed"),
                content_hash=d.get("content_hash", ""),
                created_at=str(d.get("created_at", "")),
            )
        self._registry = fresh

    def list_documents(self) -> list[dict[str, Any]]:
        if self.nexus_db:
            return self.nexus_db.list_documents()
        return [doc.to_dict() for doc in self._registry.values()]

    def get_document(self, doc_id: str) -> Optional[IngestionSummary]:
        if doc_id in self._registry:
            return self._registry[doc_id]
        if self.nexus_db:
            d = self.nexus_db.get_document(doc_id)
            if d:
                return IngestionSummary(
                    doc_id=d["doc_id"],
                    title=d["title"],
                    format=d.get("format", "text"),
                    chunk_count=d.get("chunk_count", 0),
                    triple_count=d.get("triple_count", 0),
                    entity_count=d.get("entity_count", 0),
                    duration_ms=d.get("duration_ms", 0.0),
                    status=d.get("status", "indexed"),
                    content_hash=d.get("content_hash", ""),
                    created_at=str(d.get("created_at", "")),
                )
        return None

    def get_document_by_hash(self, content_hash: str) -> Optional[IngestionSummary]:
        if not content_hash:
            return None
        for doc in self._registry.values():
            if getattr(doc, "content_hash", "") == content_hash:
                return doc
        if self.nexus_db:
            d = self.nexus_db.get_document_by_hash(content_hash)
            if d:
                return IngestionSummary(
                    doc_id=d["doc_id"],
                    title=d["title"],
                    format=d.get("format", "text"),
                    chunk_count=d.get("chunk_count", 0),
                    triple_count=d.get("triple_count", 0),
                    entity_count=d.get("entity_count", 0),
                    duration_ms=d.get("duration_ms", 0.0),
                    status=d.get("status", "indexed"),
                    content_hash=d.get("content_hash", ""),
                    created_at=str(d.get("created_at", "")),
                )
        return None

    def delete_document(self, doc_id: str) -> bool:
        with self._ingest_lock:
            return self._delete_document_locked(doc_id)

    def _delete_document_locked(self, doc_id: str) -> bool:
        existed = doc_id in self._registry or (self.nexus_db and self.nexus_db.get_document(doc_id) is not None)
        if not existed:
            return False
        self.retriever.remove_document(doc_id)
        self._registry.pop(doc_id, None)
        if self.nexus_db:
            self.nexus_db.delete_document(doc_id)
        log.info("Document %s removed from ingestion registry", doc_id)
        return True

    def _resolve_unique_doc_id(self, filename: str, doc_id: Optional[str] = None) -> str:
        if doc_id:
            return doc_id
        base_id = self.parser._clean_id(filename)
        if not self.get_document(base_id):
            return base_id
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if ext and not base_id.endswith(f"_{ext}"):
            candidate = f"{base_id}_{ext}"
            if not self.get_document(candidate):
                return candidate
            base_id = candidate
        idx = 2
        while self.get_document(f"{base_id}_{idx}"):
            idx += 1
        return f"{base_id}_{idx}"

    def ingest_document(
        self,
        raw_bytes: bytes,
        filename: str,
        format_hint: Optional[str] = None,
        doc_id: Optional[str] = None,
    ) -> IngestionSummary:
        with self._ingest_lock:
            return self._ingest_document_locked(raw_bytes, filename, format_hint, doc_id)

    def _ingest_document_locked(
        self,
        raw_bytes: bytes,
        filename: str,
        format_hint: Optional[str] = None,
        doc_id: Optional[str] = None,
    ) -> IngestionSummary:
        t0 = time.perf_counter()

        content_hash = hashlib.sha256(raw_bytes).hexdigest()
        existing = self.get_document_by_hash(content_hash)
        if existing and not doc_id:
            log.info("Document with matching hash %s already indexed as %s. Skipping duplicate.", content_hash[:8], existing.doc_id)
            return IngestionSummary(
                doc_id=existing.doc_id,
                title=existing.title,
                format=existing.format,
                chunk_count=existing.chunk_count,
                triple_count=existing.triple_count,
                entity_count=existing.entity_count,
                duration_ms=0.0,
                status="skipped_duplicate",
                content_hash=content_hash,
            )

        target_doc_id = self._resolve_unique_doc_id(filename, doc_id)
        if doc_id and self.get_document(doc_id):
            self.retriever.remove_document(doc_id)

        # 1. Parse into hierarchical AST
        parsed_doc: ParsedDocument = self.parser.parse(
            raw_bytes,
            filename=filename,
            format_hint=format_hint,
            doc_id=target_doc_id,
        )

        # 2. Contextual chunking with lineage headers
        chunks: list[IngestionChunk] = self.chunker.chunk_document(parsed_doc)

        # 3. Entity-Relation triplet extraction
        triples: list[KnowledgeTriple] = self.graph_builder.extract_triples(chunks)

        # 4. Ingest into HybridRetriever (vector store + graph store). Only these chunks are embedded and
        #    appended; existing vectors are never recomputed.
        self.retriever.ingest_contextual_chunks(chunks, triples)

        dur = (time.perf_counter() - t0) * 1000.0
        unique_entities = set(t.head for t in triples) | set(t.tail for t in triples)

        summary = IngestionSummary(
            doc_id=parsed_doc.doc_id,
            title=parsed_doc.title,
            format=parsed_doc.format.value,
            chunk_count=len(chunks),
            triple_count=len(triples),
            entity_count=len(unique_entities),
            duration_ms=round(dur, 2),
            status="indexed",
            content_hash=content_hash,
        )
        self._registry[parsed_doc.doc_id] = summary
        if self.nexus_db:
            self.nexus_db.insert_document(
                doc_id=summary.doc_id,
                title=summary.title,
                format=summary.format,
                chunk_count=summary.chunk_count,
                triple_count=summary.triple_count,
                entity_count=summary.entity_count,
                duration_ms=summary.duration_ms,
                status=summary.status,
                content_hash=summary.content_hash,
            )
        log.info(
            "Ingested %s (%s): %d chunks, %d triples in %.2f ms",
            parsed_doc.doc_id,
            parsed_doc.title,
            len(chunks),
            len(triples),
            dur,
        )
        return summary

    def ingest_file(self, file_path: Path | str, format_hint: Optional[str] = None) -> IngestionSummary:
        p = Path(file_path)
        with open(p, "rb") as f:
            raw_bytes = f.read()
        return self.ingest_document(raw_bytes, filename=p.name, format_hint=format_hint)

    def ingest_sample_pack(self, raw_dir: Optional[Path | str] = None) -> list[IngestionSummary]:
        if raw_dir is None:
            from nexus_engine.core.data_loader import get_data_dir
            raw_dir = get_data_dir() / "raw"
        else:
            raw_dir = Path(raw_dir)

        summaries: list[IngestionSummary] = []

        # 1. Legal sample (JSON)
        legal_path = raw_dir / "legal_sample" / "commercial_contract_standards.json"
        if legal_path.is_file():
            summaries.append(self.ingest_file(legal_path, format_hint="json_legal"))

        # 2. Corporate sample (Markdown SOP)
        corp_path = raw_dir / "corporate_sample" / "procurement_policy_sop.md"
        if corp_path.is_file():
            summaries.append(self.ingest_file(corp_path, format_hint="markdown"))

        # 3. Compliance sample (Text / PDF)
        comp_txt = raw_dir / "compliance_sample" / "export_control_guidelines.txt"
        if comp_txt.is_file():
            summaries.append(self.ingest_file(comp_txt, format_hint="plain_text"))

        # Auto-discover domain-specific corpora (banking, medical, chemical, etc.)
        if raw_dir.is_dir():
            for domain_dir in sorted(raw_dir.iterdir()):
                if domain_dir.is_dir() and domain_dir.name not in ("legal_sample", "corporate_sample", "compliance_sample", "archive"):
                    for doc_file in sorted(domain_dir.iterdir()):
                        if doc_file.is_file() and not doc_file.name.startswith("."):
                            ext = doc_file.suffix.lower()
                            hint = "json_legal" if ext == ".json" else "markdown" if ext in (".md", ".markdown") else "plain_text"
                            try:
                                summaries.append(self.ingest_file(doc_file, format_hint=hint))
                            except Exception as e:
                                log.warning("Failed to auto-ingest %s: %s", doc_file, e)

        return summaries

    def reset_knowledge_base(self, raw_dir: Optional[Path | str] = None) -> list[IngestionSummary]:
        with self._ingest_lock:
            return self._reset_knowledge_base_locked(raw_dir)

    def _reset_knowledge_base_locked(self, raw_dir: Optional[Path | str] = None) -> list[IngestionSummary]:
        if self.nexus_db:
            self.nexus_db.reset_database()
        self._registry.clear()
        self.retriever.clear()

        from nexus_engine.services.retrieval.corpus import SAMPLE_DOCUMENTS
        for doc_id, raw in SAMPLE_DOCUMENTS.items():
            self.retriever.index_document(doc_id, raw)

        if self.nexus_db:
            with self.nexus_db._get_conn() as conn:
                from nexus_engine.services.storage.operations import backfill_documents_if_empty
                backfill_documents_if_empty(conn)

        summaries = self.ingest_sample_pack(raw_dir)
        self._rehydrate_registry()
        return summaries

