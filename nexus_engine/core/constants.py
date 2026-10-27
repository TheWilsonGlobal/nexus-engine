"""System-wide default constants and architectural configurations for Nexus-Engine."""
from __future__ import annotations

# Application Version
VERSION = "0.3.0"

# Storage & Database Paths
from pathlib import Path
_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_AUDIT_DB_PATH = str(_REPO_ROOT / "data" / "db" / "audit.db")
DEFAULT_DATABASE_PATH = str(_REPO_ROOT / "data" / "db" / "nexus.db")

# Server & Networking
DEFAULT_BACKEND_HOST = "127.0.0.1"
DEFAULT_BACKEND_PORT = 8766
DEFAULT_FRONTEND_PORT = 5200
DEFAULT_CORS_ORIGINS = ("http://127.0.0.1:5200", "http://localhost:5200", "http://127.0.0.1:5199", "http://localhost:5199")
DEFAULT_NETWORK_ENABLED = True
DEFAULT_NETWORK_ALLOW_PRIVATE_IPS = False
DEFAULT_NETWORK_ALLOWED_DOMAINS = ("*",)
DEFAULT_WEB_SEARCH_PROVIDER = "duckduckgo"

# Sandbox Execution Limits
DEFAULT_SANDBOX_ENABLED = False  # code execution is OFF unless an operator opts in (admin-only even then)
DEFAULT_SANDBOX_TIMEOUT_S = 5.0
DEFAULT_SANDBOX_MEMORY_MB = 256
DEFAULT_SANDBOX_MAX_CONCURRENT = 2
DEFAULT_SANDBOX_NETWORK_MODE = "none"

# Memory & Session Limits
DEFAULT_SHORT_TERM_MAX_TURNS = 8
DEFAULT_SHORT_TERM_MAX_TOKENS = 512
DEFAULT_MAX_SESSIONS = 1000
DEFAULT_SESSION_TTL_S = 3600.0

# Cache & Precedent Management
DEFAULT_CACHE_THRESHOLD = 0.92
DEFAULT_CACHE_MAX_ENTRIES = 256
DEFAULT_PRECEDENT_PROMOTE_AFTER = 3

# Agent Execution Bounds
DEFAULT_MAX_AGENT_STEPS = 4
DEFAULT_LOG_LEVEL = "INFO"

# Vector Embeddings Subsystem
DEFAULT_EMBEDDING_PROVIDER = "gemini"  # "gemini" | "remote" | "mock"
DEFAULT_EMBEDDING_URL = "http://127.0.0.1:5819/v1/embeddings"  # loopback; override with EMBEDDING_URL
DEFAULT_EMBEDDING_MODEL = "text-embedding-004"
DEFAULT_EMBEDDING_DIM = 768

# LLM Reasoning Engine Configuration
DEFAULT_LLM_PROVIDER = "gemini"  # "gemini" | "ollama" | "mock"
DEFAULT_LLM_MODEL = "gemini-3.5-flash-lite"
DEFAULT_LLM_TEMPERATURE = 0.2
DEFAULT_LLM_LOCAL_URL = "http://127.0.0.1:11434/v1"  # loopback (Ollama default); override with LLM_LOCAL_URL
DEFAULT_LLM_LOCAL_MODEL = "llama3.2:3b"

# Observability & Token Budget
DEFAULT_TOKEN_BUDGET = 8192
DEFAULT_MAX_HISTORY_TOKENS = 1500
DEFAULT_MAX_OBSERVATION_TOKENS = 6000
DEFAULT_LANGFUSE_BASE_URL = "https://cloud.langfuse.com"

# API Defense, Rate Limiting & Auth Configuration
DEFAULT_RATE_LIMIT_ENABLED = True
DEFAULT_MAX_PAYLOAD_SIZE_MB = 10
DEFAULT_MAX_JSON_SIZE_BYTES = 1048576  # 1 MB
DEFAULT_AUTH_SESSION_TTL_S = 86400.0  # 24 hours
DEFAULT_AUTH_SESSION_IDLE_S = 7200.0  # 2 hours
