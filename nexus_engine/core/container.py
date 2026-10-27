"""Dependency container: builds a fresh, isolated set of services (no module globals)."""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from nexus_engine.core.config import Settings
from nexus_engine.core.embeddings import build_embedder
from nexus_engine.services.agent.budget import TokenBudgetManager
from nexus_engine.services.agent.compressor import ContextCompressor
from nexus_engine.services.agent.loop import AgentLoop
from nexus_engine.services.agent.providers import LLMClient, create_llm_client
from nexus_engine.services.agent.tools import default_registry
from nexus_engine.services.guardrail.deep_verifier import DeepVerifier
from nexus_engine.services.guardrail.router import Guardrail
from nexus_engine.services.ingestion.pipeline import IngestionPipeline
from nexus_engine.services.memory.manager import MemoryManager
from nexus_engine.services.memory.persistent_audit import PersistentAudit
from nexus_engine.services.memory.semantic_cache import SemanticCache
from nexus_engine.services.network.http_client import SafeHTTPClient
from nexus_engine.services.network.ssrf_guard import SSRFGuard
from nexus_engine.services.observability.config import ObservabilityConfig
from nexus_engine.services.observability.tracer import ObservabilityTracer
from nexus_engine.services.retrieval.hybrid_retriever import HybridRetriever
from nexus_engine.services.sandbox.executor import SandboxExecutor
from nexus_engine.services.security.auth_manager import AuthManager
from nexus_engine.services.security.rate_limiter import SlidingWindowRateLimiter
from nexus_engine.services.storage.database import NexusDatabase


@dataclass
class Container:
    settings: Settings
    memory: MemoryManager
    retriever: HybridRetriever
    guardrail: Guardrail
    sandbox: SandboxExecutor
    agent: AgentLoop
    pipeline: IngestionPipeline
    nexus_db: NexusDatabase
    http_client: SafeHTTPClient
    llm_client: LLMClient
    compressor: ContextCompressor
    budget_mgr: TokenBudgetManager
    tracer: ObservabilityTracer
    auth_mgr: AuthManager = field(default_factory=AuthManager)
    rate_limiter: SlidingWindowRateLimiter = field(default_factory=SlidingWindowRateLimiter)
    started_at: float = field(default_factory=time.monotonic)


def build_container(settings: Settings) -> Container:
    embedder = build_embedder(settings)  # one embedder (one circuit breaker, one pinned dimension) for all consumers
    memory = MemoryManager(
        PersistentAudit(settings.audit_db_path),
        SemanticCache(settings.cache_threshold, settings.cache_max_entries, embedder=embedder),
        settings.short_term_max_turns,
        settings.short_term_max_tokens,
        settings.precedent_promote_after,
        settings.max_sessions,
        settings.session_ttl_s,
    )
    # Embedded Database
    nexus_db = NexusDatabase(settings.database_path)

    retriever = HybridRetriever.with_sample_corpus(nexus_db=nexus_db, embedder=embedder)
    pipeline = IngestionPipeline(retriever, nexus_db=nexus_db)
    sandbox = SandboxExecutor(
        settings.sandbox_timeout_s,
        settings.sandbox_memory_mb,
        max_concurrent=settings.sandbox_max_concurrent,
        enabled=settings.sandbox_enabled,
    )

    # Controlled Network Subsystem
    ssrf_guard = SSRFGuard(
        allowed_domains=settings.network_allowed_domains,
        allow_private_ips=settings.network_allow_private_ips,
        enabled=settings.network_enabled,
    )
    http_client = SafeHTTPClient(guard=ssrf_guard)

    # Pluggable LLM Client
    llm_client = create_llm_client(
        provider=settings.llm_provider,
        api_key=settings.gemini_api_key,
        model_name=settings.gemini_model_name,
        local_url=settings.llm_local_url,
    )

    # Dual-Process Guardrail with LLM Deep Verifier
    deep_verifier = DeepVerifier(llm_client=llm_client)
    guardrail = Guardrail(verifier=deep_verifier)

    # Tool Registry with web_search and http_fetch
    tools = default_registry(retriever, sandbox, memory, http_client=http_client)

    compressor = ContextCompressor(
        max_budget=settings.token_budget,
        max_history_tokens=settings.max_history_tokens,
        max_observation_tokens=settings.max_observation_tokens,
    )
    budget_mgr = TokenBudgetManager(
        budget_limit=settings.token_budget,
        default_model=settings.llm_model,
    )
    obs_config = ObservabilityConfig(
        public_key=settings.langfuse_public_key,
        secret_key=settings.langfuse_secret_key,
        base_url=settings.langfuse_base_url,
        enabled=bool(settings.langfuse_public_key and settings.langfuse_secret_key),
    )
    tracer = ObservabilityTracer(config=obs_config)

    auth_mgr = AuthManager(
        ttl_s=settings.auth_session_ttl_s,
        idle_timeout_s=settings.auth_session_idle_s,
    )
    rate_limiter = SlidingWindowRateLimiter(enabled=settings.rate_limit_enabled)

    agent = AgentLoop(
        guardrail,
        memory,
        tools,
        sandbox,
        max_steps=settings.max_agent_steps,
        llm_client=llm_client,
        compressor=compressor,
        budget_mgr=budget_mgr,
        tracer=tracer,
    )
    return Container(
        settings,
        memory,
        retriever,
        guardrail,
        sandbox,
        agent,
        pipeline,
        nexus_db,
        http_client,
        llm_client,
        compressor,
        budget_mgr,
        tracer,
        auth_mgr=auth_mgr,
        rate_limiter=rate_limiter,
    )
