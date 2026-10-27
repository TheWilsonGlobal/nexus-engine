"""Runtime configuration, read from environment variables (supporting with or without COGNI_ prefix)."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, TypeVar

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

_REPO_ROOT = Path(__file__).resolve().parents[3]

from nexus_engine.core.constants import (
    DEFAULT_AUDIT_DB_PATH,
    DEFAULT_AUTH_SESSION_IDLE_S,
    DEFAULT_AUTH_SESSION_TTL_S,
    DEFAULT_BACKEND_HOST,
    DEFAULT_BACKEND_PORT,
    DEFAULT_CACHE_MAX_ENTRIES,
    DEFAULT_CACHE_THRESHOLD,
    DEFAULT_CORS_ORIGINS,
    DEFAULT_DATABASE_PATH,
    DEFAULT_EMBEDDING_DIM,
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_EMBEDDING_PROVIDER,
    DEFAULT_EMBEDDING_URL,
    DEFAULT_FRONTEND_PORT,
    DEFAULT_LLM_MODEL,
    DEFAULT_LANGFUSE_BASE_URL,
    DEFAULT_LLM_LOCAL_MODEL,
    DEFAULT_LLM_LOCAL_URL,
    DEFAULT_LLM_PROVIDER,
    DEFAULT_LLM_TEMPERATURE,
    DEFAULT_LOG_LEVEL,
    DEFAULT_MAX_AGENT_STEPS,
    DEFAULT_MAX_HISTORY_TOKENS,
    DEFAULT_MAX_JSON_SIZE_BYTES,
    DEFAULT_MAX_OBSERVATION_TOKENS,
    DEFAULT_MAX_PAYLOAD_SIZE_MB,
    DEFAULT_MAX_SESSIONS,
    DEFAULT_NETWORK_ALLOW_PRIVATE_IPS,
    DEFAULT_NETWORK_ALLOWED_DOMAINS,
    DEFAULT_NETWORK_ENABLED,
    DEFAULT_PRECEDENT_PROMOTE_AFTER,
    DEFAULT_RATE_LIMIT_ENABLED,
    DEFAULT_SANDBOX_ENABLED,
    DEFAULT_SANDBOX_MAX_CONCURRENT,
    DEFAULT_SANDBOX_MEMORY_MB,
    DEFAULT_SANDBOX_NETWORK_MODE,
    DEFAULT_SANDBOX_TIMEOUT_S,
    DEFAULT_SESSION_TTL_S,
    DEFAULT_SHORT_TERM_MAX_TOKENS,
    DEFAULT_SHORT_TERM_MAX_TURNS,
    DEFAULT_TOKEN_BUDGET,
    DEFAULT_WEB_SEARCH_PROVIDER,
    VERSION,
)

log = logging.getLogger(__name__)
T = TypeVar("T", int, float)


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    """Read an environment variable checking unprefixed first, then NEXUS_ or COGNI_ prefixed."""
    val = os.environ.get(name)
    if val is not None and val.strip() != "":
        return val
    nexus_val = os.environ.get(f"NEXUS_{name}")
    if nexus_val is not None and nexus_val.strip() != "":
        return nexus_val
    cogni_val = os.environ.get(f"COGNI_{name}")
    if cogni_val is not None and cogni_val.strip() != "":
        return cogni_val
    return default


def _num(name: str, default: T, cast: Callable[[str], T]) -> T:
    """Read a positive number from the environment; bad/non-positive values fall back to default."""
    raw = _env(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        val = cast(raw)
        if val <= 0:
            raise ValueError("must be > 0")
        return val
    except ValueError:
        log.warning("ignoring invalid %s=%r; using default %r", name, raw, default)
        return default


@dataclass(frozen=True)
class Settings:
    audit_db_path: str = DEFAULT_AUDIT_DB_PATH
    cors_origins: tuple[str, ...] = DEFAULT_CORS_ORIGINS
    sandbox_enabled: bool = DEFAULT_SANDBOX_ENABLED
    sandbox_timeout_s: float = DEFAULT_SANDBOX_TIMEOUT_S
    sandbox_memory_mb: int = DEFAULT_SANDBOX_MEMORY_MB
    sandbox_max_concurrent: int = DEFAULT_SANDBOX_MAX_CONCURRENT
    short_term_max_turns: int = DEFAULT_SHORT_TERM_MAX_TURNS
    short_term_max_tokens: int = DEFAULT_SHORT_TERM_MAX_TOKENS
    max_sessions: int = DEFAULT_MAX_SESSIONS
    session_ttl_s: float = DEFAULT_SESSION_TTL_S
    cache_threshold: float = DEFAULT_CACHE_THRESHOLD
    cache_max_entries: int = DEFAULT_CACHE_MAX_ENTRIES
    precedent_promote_after: int = DEFAULT_PRECEDENT_PROMOTE_AFTER
    max_agent_steps: int = DEFAULT_MAX_AGENT_STEPS
    api_key: Optional[str] = None  # None = open (local dev); set API_KEY to require it
    log_level: str = DEFAULT_LOG_LEVEL
    version: str = VERSION
    embedding_provider: str = DEFAULT_EMBEDDING_PROVIDER
    embedding_url: str = DEFAULT_EMBEDDING_URL
    embedding_model: str = DEFAULT_EMBEDDING_MODEL
    embedding_dim: int = DEFAULT_EMBEDDING_DIM
    llm_provider: str = DEFAULT_LLM_PROVIDER
    gemini_api_key: Optional[str] = None
    gemini_embedding_api_key: Optional[str] = None
    llm_model: str = DEFAULT_LLM_MODEL
    gemini_model_name: str = DEFAULT_LLM_MODEL
    llm_temperature: float = DEFAULT_LLM_TEMPERATURE
    llm_local_url: str = DEFAULT_LLM_LOCAL_URL
    llm_local_model: str = DEFAULT_LLM_LOCAL_MODEL
    database_path: str = DEFAULT_DATABASE_PATH
    network_enabled: bool = DEFAULT_NETWORK_ENABLED
    network_allow_private_ips: bool = DEFAULT_NETWORK_ALLOW_PRIVATE_IPS
    network_allowed_domains: tuple[str, ...] = DEFAULT_NETWORK_ALLOWED_DOMAINS
    sandbox_network_mode: str = DEFAULT_SANDBOX_NETWORK_MODE
    web_search_provider: str = DEFAULT_WEB_SEARCH_PROVIDER
    backend_host: str = DEFAULT_BACKEND_HOST
    backend_port: int = DEFAULT_BACKEND_PORT
    frontend_port: int = DEFAULT_FRONTEND_PORT
    langfuse_public_key: Optional[str] = None
    langfuse_secret_key: Optional[str] = None
    langfuse_base_url: str = DEFAULT_LANGFUSE_BASE_URL
    token_budget: int = DEFAULT_TOKEN_BUDGET
    max_history_tokens: int = DEFAULT_MAX_HISTORY_TOKENS
    max_observation_tokens: int = DEFAULT_MAX_OBSERVATION_TOKENS
    rate_limit_enabled: bool = DEFAULT_RATE_LIMIT_ENABLED
    max_payload_size_mb: int = DEFAULT_MAX_PAYLOAD_SIZE_MB
    max_json_size_bytes: int = DEFAULT_MAX_JSON_SIZE_BYTES
    auth_session_ttl_s: float = DEFAULT_AUTH_SESSION_TTL_S
    auth_session_idle_s: float = DEFAULT_AUTH_SESSION_IDLE_S

    def __post_init__(self) -> None:
        if not Path(self.database_path).is_absolute():
            object.__setattr__(self, "database_path", str(_REPO_ROOT / self.database_path))
        if not Path(self.audit_db_path).is_absolute():
            object.__setattr__(self, "audit_db_path", str(_REPO_ROOT / self.audit_db_path))

    @classmethod
    def from_env(cls) -> "Settings":
        origins = _env("DEFAULT_CORS_ORIGINS") or _env("CORS_ORIGINS")
        domains = _env("DEFAULT_NETWORK_ALLOWED_DOMAINS") or _env("NETWORK_ALLOWED_DOMAINS")
        model = (
            _env("LLM_MODEL")
            or _env("GEMINI_MODEL")
            or _env("GEMINI_MODEL_NAME", DEFAULT_LLM_MODEL)
        )
        return cls(
            audit_db_path=_env("DEFAULT_AUDIT_DB_PATH") or _env("AUDIT_DB_PATH", DEFAULT_AUDIT_DB_PATH),  # type: ignore[arg-type]
            cors_origins=tuple(o.strip() for o in origins.split(",") if o.strip())
            if origins
            else DEFAULT_CORS_ORIGINS,
            sandbox_enabled=_env("SANDBOX_ENABLED", str(DEFAULT_SANDBOX_ENABLED)).lower() in ("true", "1", "yes"),
            sandbox_timeout_s=_num("SANDBOX_TIMEOUT_S", DEFAULT_SANDBOX_TIMEOUT_S, float),
            sandbox_memory_mb=_num("SANDBOX_MEMORY_MB", DEFAULT_SANDBOX_MEMORY_MB, int),
            sandbox_max_concurrent=_num("SANDBOX_MAX_CONCURRENT", DEFAULT_SANDBOX_MAX_CONCURRENT, int),
            short_term_max_turns=_num("SHORT_TERM_MAX_TURNS", DEFAULT_SHORT_TERM_MAX_TURNS, int),
            short_term_max_tokens=_num("SHORT_TERM_MAX_TOKENS", DEFAULT_SHORT_TERM_MAX_TOKENS, int),
            max_sessions=_num("MAX_SESSIONS", DEFAULT_MAX_SESSIONS, int),
            session_ttl_s=_num("SESSION_TTL_S", DEFAULT_SESSION_TTL_S, float),
            cache_threshold=min(1.0, _num("CACHE_THRESHOLD", DEFAULT_CACHE_THRESHOLD, float)),
            cache_max_entries=_num("CACHE_MAX_ENTRIES", DEFAULT_CACHE_MAX_ENTRIES, int),
            precedent_promote_after=_num("PRECEDENT_PROMOTE_AFTER", DEFAULT_PRECEDENT_PROMOTE_AFTER, int),
            max_agent_steps=_num("MAX_AGENT_STEPS", DEFAULT_MAX_AGENT_STEPS, int),
            api_key=_env("API_KEY") or None,
            log_level=_env("LOG_LEVEL", DEFAULT_LOG_LEVEL),  # type: ignore[arg-type]
            embedding_provider=_env("EMBEDDING_PROVIDER", DEFAULT_EMBEDDING_PROVIDER),  # type: ignore[arg-type]
            embedding_url=_env("DEFAULT_EMBEDDING_URL") or _env("EMBEDDING_URL", DEFAULT_EMBEDDING_URL),  # type: ignore[arg-type]
            embedding_model=_env("EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL),  # type: ignore[arg-type]
            embedding_dim=_num("EMBEDDING_DIM", DEFAULT_EMBEDDING_DIM, int),
            llm_provider=_env("LLM_PROVIDER", DEFAULT_LLM_PROVIDER),  # type: ignore[arg-type]
            gemini_api_key=_env("GEMINI_LLM__API_KEY") or _env("GEMINI_LLM_API_KEY") or _env("GEMINI_API_KEY"),
            gemini_embedding_api_key=_env("GEMINI_EMBEDDING_API_KEY") or _env("GEMINI_API_KEY") or _env("GEMINI_LLM__API_KEY") or _env("GEMINI_LLM_API_KEY"),
            llm_model=model,  # type: ignore[arg-type]
            gemini_model_name=model,  # type: ignore[arg-type]
            llm_temperature=_num("LLM_TEMPERATURE", DEFAULT_LLM_TEMPERATURE, float),
            llm_local_url=_env("DEFAULT_LLM_LOCAL_URL") or _env("LLM_LOCAL_URL", DEFAULT_LLM_LOCAL_URL),  # type: ignore[arg-type]
            llm_local_model=_env("DEFAULT_LLM_LOCAL_MODEL") or _env("LLM_LOCAL_MODEL", DEFAULT_LLM_LOCAL_MODEL),  # type: ignore[arg-type]
            database_path=_env("DEFAULT_DATABASE_PATH") or _env("DATABASE_PATH", DEFAULT_DATABASE_PATH),  # type: ignore[arg-type]
            network_enabled=(_env("NETWORK_ENABLED", str(DEFAULT_NETWORK_ENABLED))).lower() in ("true", "1", "yes"),
            network_allow_private_ips=(_env("NETWORK_ALLOW_PRIVATE_IPS", str(DEFAULT_NETWORK_ALLOW_PRIVATE_IPS))).lower() in ("true", "1", "yes"),
            network_allowed_domains=tuple(d.strip() for d in domains.split(",") if d.strip()) if domains else DEFAULT_NETWORK_ALLOWED_DOMAINS,
            sandbox_network_mode=_env("SANDBOX_NETWORK_MODE", DEFAULT_SANDBOX_NETWORK_MODE),  # type: ignore[arg-type]
            web_search_provider=_env("WEB_SEARCH_PROVIDER", DEFAULT_WEB_SEARCH_PROVIDER),  # type: ignore[arg-type]
            backend_host=_env("BACKEND_HOST", DEFAULT_BACKEND_HOST),  # type: ignore[arg-type]
            backend_port=_num("BACKEND_PORT", DEFAULT_BACKEND_PORT, int),
            frontend_port=_num("FRONTEND_PORT", DEFAULT_FRONTEND_PORT, int),
            langfuse_public_key=_env("LANGFUSE_PUBLIC_KEY"),
            langfuse_secret_key=_env("LANGFUSE_SECRET_KEY"),
            langfuse_base_url=_env("LANGFUSE_BASE_URL", DEFAULT_LANGFUSE_BASE_URL) or DEFAULT_LANGFUSE_BASE_URL,
            token_budget=_num("TOKEN_BUDGET", DEFAULT_TOKEN_BUDGET, int),
            max_history_tokens=_num("MAX_HISTORY_TOKENS", DEFAULT_MAX_HISTORY_TOKENS, int),
            max_observation_tokens=_num("MAX_OBSERVATION_TOKENS", DEFAULT_MAX_OBSERVATION_TOKENS, int),
            rate_limit_enabled=(_env("RATE_LIMIT_ENABLED", str(DEFAULT_RATE_LIMIT_ENABLED))).lower() in ("true", "1", "yes"),
            max_payload_size_mb=_num("MAX_PAYLOAD_SIZE_MB", DEFAULT_MAX_PAYLOAD_SIZE_MB, int),
            max_json_size_bytes=_num("MAX_JSON_SIZE_BYTES", DEFAULT_MAX_JSON_SIZE_BYTES, int),
            auth_session_ttl_s=_num("AUTH_SESSION_TTL_S", DEFAULT_AUTH_SESSION_TTL_S, float),
            auth_session_idle_s=_num("AUTH_SESSION_IDLE_S", DEFAULT_AUTH_SESSION_IDLE_S, float),
        )
