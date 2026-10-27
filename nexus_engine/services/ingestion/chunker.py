"""Semantic & Parent-Child contextual chunking preserving hierarchical breadcrumbs."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from nexus_engine.core.embeddings import tokenize
from nexus_engine.services.ingestion.parser import ParsedDocument, ParsedSection


@dataclass
class IngestionChunk:
    id: str
    doc_id: str
    parent_id: Optional[str]
    level: str
    title: str
    text: str
    context_header: str
    full_text: str
    tokens: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return self.title or self.id.rsplit(":", 1)[-1]


class ContextualChunker:
    def __init__(self, max_tokens: int = 500) -> None:
        self.max_tokens = max_tokens

    def chunk_document(self, doc: ParsedDocument) -> list[IngestionChunk]:
        chunks: list[IngestionChunk] = []

        for sec in doc.sections:
            self._recurse_section(doc, sec, parent_id=None, chunks=chunks)

        return chunks

    def _recurse_section(
        self,
        doc: ParsedDocument,
        sec: ParsedSection,
        parent_id: Optional[str],
        chunks: list[IngestionChunk],
    ) -> None:
        # Build breadcrumb context header: e.g. "Document: Civil Code > Chapter I > Article 1"
        header = f"Document: {doc.title}"
        if sec.path:
            header += " > " + " > ".join(sec.path)

        # If section has subsections, recurse down to leaves
        if sec.subsections:
            for sub in sec.subsections:
                self._recurse_section(doc, sub, parent_id=sec.section_id, chunks=chunks)
            # If section also has its own content, create a chunk for it
            if sec.content.strip():
                c = self._make_chunk(doc.doc_id, sec, parent_id, header, sec.content.strip())
                chunks.append(c)
        else:
            # Leaf section
            body = sec.content.strip()
            if body:
                c = self._make_chunk(doc.doc_id, sec, parent_id, header, body)
                chunks.append(c)
            elif sec.title:
                # Stub leaf
                c = self._make_chunk(doc.doc_id, sec, parent_id, header, sec.title)
                chunks.append(c)

    def _make_chunk(
        self,
        doc_id: str,
        sec: ParsedSection,
        parent_id: Optional[str],
        context_header: str,
        text: str,
    ) -> IngestionChunk:
        full_text = f"{context_header}\n{text}".strip()
        tokens = tokenize(full_text)
        return IngestionChunk(
            id=sec.section_id,
            doc_id=doc_id,
            parent_id=parent_id,
            level=sec.level,
            title=sec.title,
            text=text,
            context_header=context_header,
            full_text=full_text,
            tokens=tokens,
            metadata=dict(sec.metadata),
        )
