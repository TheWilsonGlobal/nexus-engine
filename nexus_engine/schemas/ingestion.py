"""Pydantic schemas for document ingestion and knowledge registry."""
from __future__ import annotations

from typing import Any, Optional
from pydantic import BaseModel


class IngestionResponse(BaseModel):
    doc_id: str
    title: str
    format: str
    chunk_count: int
    triple_count: int
    entity_count: int
    duration_ms: float
    status: str
    created_at: str


class DocumentMetaItem(BaseModel):
    doc_id: str
    title: str
    format: str
    chunk_count: int
    triple_count: int
    entity_count: int
    duration_ms: float
    status: str
    created_at: str
    vector_count: Optional[int] = 0
    node_count: Optional[int] = 0
    edge_count: Optional[int] = 0


class DocumentListResponse(BaseModel):
    documents: list[DocumentMetaItem]
    total_documents: int
    total_chunks: int
    total_triples: int


class SamplePackResponse(BaseModel):
    ingested_documents: list[IngestionResponse]
    total_chunks: int
    total_triples: int
    total_duration_ms: float


class DuplicateDocItem(BaseModel):
    doc_id: str
    title: str
    chunk_count: int
    created_at: str
    action: str


class DuplicateDocGroup(BaseModel):
    canonical_title: str
    primary_doc_id: str
    duplicates: list[DuplicateDocItem]


class DuplicateChunkItem(BaseModel):
    chunk_id: str
    doc_id: str
    path: str
    snippet: str


class DuplicateVectorItem(BaseModel):
    chunk_id: str
    dimension: int


class MergeableNodeGroup(BaseModel):
    label: str
    entity_type: str
    canonical_id: str
    duplicate_ids: list[str]


class RedundantEdgeItem(BaseModel):
    source_id: str
    target_id: str
    relation: str
    count: int


class RefactorAnalysisResponse(BaseModel):
    has_duplicates: bool
    duplicate_documents_count: int
    duplicate_chunks_count: int
    duplicate_vectors_count: int
    mergeable_nodes_count: int
    redundant_edges_count: int
    duplicate_groups: list[DuplicateDocGroup] = []
    duplicate_chunks: list[DuplicateChunkItem] = []
    duplicate_vectors: list[DuplicateVectorItem] = []
    mergeable_nodes: list[MergeableNodeGroup] = []
    redundant_edges: list[RedundantEdgeItem] = []
    current_stats: dict[str, int]
    projected_stats: dict[str, int]


class RefactorApplyResponse(BaseModel):
    status: str
    removed_docs: int
    removed_chunks: int
    removed_vectors: int
    merged_nodes: int
    removed_edges: int
    final_stats: dict[str, int]



class KnowledgeExportGraph(BaseModel):
    nodes: list[dict[str, Any]]
    edges: list[dict[str, Any]]


class KnowledgeExportResponse(BaseModel):
    """Whole-KB snapshot. Embeddings are excluded; originals are not stored, so this is not a restore format."""

    version: int
    exported_at: str
    stats: dict[str, Any]
    documents: list[dict[str, Any]]
    chunks: list[dict[str, Any]]
    graph: KnowledgeExportGraph
