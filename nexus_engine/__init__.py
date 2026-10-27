"""Nexus Engine: Enterprise Agentic AI Platform & SDK.

Provides core abstractions for:
- ReAct Agent Reasoning Loop & Tool Orchestration
- Dual-Process Safety Guardrail (<20ms Fast Filter + LLM Deep Verifier)
- Hybrid Vector ANN & SQL Recursive CTE Knowledge Graph Retrieval
- Token Optimization, Context Compression & Langfuse Observability
- Tamper-Evident SHA-256 Audit Ledger & Security Sandbox
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

__version__ = "0.2.0"
log = logging.getLogger("nexus_engine")

# -----------------------------------------------------------------------------
# Core SDK Exports
# -----------------------------------------------------------------------------
from nexus_engine.core.config import Settings
from nexus_engine.core.container import Container, build_container
from nexus_engine.services.agent.loop import AgentLoop
from nexus_engine.services.guardrail.fast_filter import FastResult, fast_filter, scan_signals, combine
from nexus_engine.services.guardrail.deep_verifier import DeepVerifier
from nexus_engine.services.guardrail.router import Guardrail, GuardrailDecision, route_for_risk
from nexus_engine.services.retrieval.hybrid_retriever import HybridRetriever
from nexus_engine.services.retrieval.vector_store import VectorStore
from nexus_engine.services.retrieval.graph_store import GraphStore
from nexus_engine.services.memory.persistent_audit import PersistentAudit
from nexus_engine.services.memory.semantic_cache import SemanticCache
from nexus_engine.services.observability.tracer import ObservabilityTracer
Tracer = ObservabilityTracer
from nexus_engine.services.sandbox.executor import SandboxExecutor
from nexus_engine.services.network.http_client import SafeHTTPClient
SafeHttpClient = SafeHTTPClient
from nexus_engine.services.ingestion.pipeline import IngestionPipeline
from nexus_engine.services.ingestion.parser import HierarchicalParser
from nexus_engine.services.ingestion.parser_models import DocFormat, ParsedDocument, ParsedSection

# -----------------------------------------------------------------------------
# Extension Interfaces for Domain Solutions (Banking, Medical, Chemical)
# -----------------------------------------------------------------------------
class BaseDocumentParser(ABC):
    """Abstract base class for domain-specific document ingestion parsers."""

    @abstractmethod
    def parse(self, raw_bytes: bytes, base_id: str, filename: str) -> ParsedDocument:
        """Parse raw file bytes into a structured ParsedDocument."""
        raise NotImplementedError


class BaseRuleProvider(ABC):
    """Abstract base class for domain guardrail rule providers."""

    @abstractmethod
    def load_rules(self) -> list[tuple[str, Any, float]]:
        """Load list of (signal_name, compiled_regex_or_predicate, weight)."""
        raise NotImplementedError


__all__ = [
    "__version__",
    "Settings",
    "Container",
    "build_container",
    "AgentLoop",
    "fast_filter",
    "FastResult",
    "scan_signals",
    "combine",
    "DeepVerifier",
    "Guardrail",
    "GuardrailDecision",
    "route_for_risk",
    "HybridRetriever",
    "VectorStore",
    "GraphStore",
    "PersistentAudit",
    "SemanticCache",
    "ObservabilityTracer",
    "Tracer",
    "SandboxExecutor",
    "SafeHTTPClient",
    "SafeHttpClient",
    "IngestionPipeline",
    "HierarchicalParser",
    "DocFormat",
    "ParsedDocument",
    "ParsedSection",
    "BaseDocumentParser",
    "BaseRuleProvider",
]
