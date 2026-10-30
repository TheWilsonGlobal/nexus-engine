# Configuration & Deployment Reference

Nexus Engine is configured through environment variables. The configuration loader automatically reads from a local `.env` file upon startup (via `python-dotenv`) and supports both modern unprefixed names (e.g. `LLM_PROVIDER`) and legacy `COGNI_` prefixed names (e.g. `COGNI_LLM_PROVIDER`).

Copy [`.env.example`](../.env.example) to `.env` in the repository root to configure your environment.

---

## Environment Variable Reference

### 1. Server & Authentication

| Variable | Default | Description |
| :--- | :--- | :--- |
| `BACKEND_HOST` | `127.0.0.1` | Network interface IP for the FastAPI backend. |
| `BACKEND_PORT` | `8766` | TCP port for the FastAPI backend server. |
| `FRONTEND_PORT` | `5200` | TCP port for the Vite frontend development server. |
| `CORS_ORIGINS` / `DEFAULT_CORS_ORIGINS` | `http://127.0.0.1:5200,http://localhost:5200` | Comma-separated list of allowed browser origins for CORS. |
| `ALLOWED_HOSTS` | *(unset)* | Extra comma-separated `Host` header names the API answers to, on top of `127.0.0.1`, `localhost`, `::1` and `BACKEND_HOST`. Any other Host gets `400` (DNS-rebinding defence). Set `ALLOWED_HOSTS=backend` when the Vite container proxies to `http://backend:8766` under docker compose, or to the public hostname of a deployment. `*` is ignored. |
| `API_KEY` / `COGNI_API_KEY` | *(unset)* | Shared secret key for API authentication. When unset, the API operates in **open mode** for local development. |

---

### 2. LLM Provider Configuration

| Variable | Default | Description |
| :--- | :--- | :--- |
| `LLM_PROVIDER` | `gemini` | Primary LLM provider: `gemini` (Google Gemini API), `openai_compatible` (local Ollama/vLLM), or `mock` (deterministic templates). |
| `LLM_MODEL` / `GEMINI_MODEL_NAME` | `gemini-3.5-flash-lite` | Model identifier to request from the LLM provider. |
| `GEMINI_LLM_API_KEY` / `GEMINI_API_KEY` | *(unset)* | Google Gemini API key used for agent reasoning and tool synthesis. |
| `LLM_TEMPERATURE` | `0.1` | Sampling temperature for the LLM. |
| `LLM_LOCAL_URL` | `http://127.0.0.1:11434/v1` | Base URL for OpenAI-compatible or Ollama local servers. |
| `LLM_LOCAL_MODEL` | `llama3.2:latest` | Model identifier for local OpenAI-compatible endpoints. |

---

### 3. Embedding Subsystem

| Variable | Default | Description |
| :--- | :--- | :--- |
| `EMBEDDING_PROVIDER` | `gemini` | Embedding backend: `gemini` (Google text-embedding-004), `remote` (local ONNX/DirectML server), or `mock` (hashed bag-of-words). |
| `EMBEDDING_MODEL` | `text-embedding-004` | Embedding model identifier. |
| `GEMINI_EMBEDDING_API_KEY` | *(falls back to `GEMINI_API_KEY`)* | API key dedicated to embedding requests. |
| `EMBEDDING_URL` | `http://127.0.0.1:5819/v1/embeddings` | URL for remote ONNX embedding server (`multilingual-e5-base`). |
| `EMBEDDING_DIM` | `768` | Embedding vector dimensions (768 for Gemini/E5, 384 for MiniLM). |

---

### 4. Storage & Database Paths

| Variable | Default | Description |
| :--- | :--- | :--- |
| `DATABASE_PATH` / `DEFAULT_DATABASE_PATH` | `data/db/nexus.db` | Embedded SQLite database file storing vector embeddings, recursive CTE knowledge graph, and Tier 2 semantic cache. |
| `AUDIT_DB_PATH` / `DEFAULT_AUDIT_DB_PATH` | `data/db/audit.db` | SQLite database file for Tier 3 cryptographic SHA-256 audit ledger. |

---

### 5. Memory & Semantic Cache

| Variable | Default | Description |
| :--- | :--- | :--- |
| `SHORT_TERM_MAX_TURNS` | `8` | Maximum conversation turns kept in Tier 1 short-term sliding window. |
| `SHORT_TERM_MAX_TOKENS` | `512` | Token truncation cap for short-term conversation context. |
| `MAX_SESSIONS` | `1000` | Maximum active concurrent sessions tracked in memory. |
| `SESSION_TTL_S` | `3600.0` | Inactivity TTL (seconds) before a session is evicted and promoted to audit. |
| `CACHE_THRESHOLD` | `0.92` | Cosine similarity threshold for Tier 2 vector semantic cache hit. |
| `CACHE_MAX_ENTRIES` | `256` | Maximum entries stored in the Tier 2 LRU semantic cache. |
| `PRECEDENT_PROMOTE_AFTER` | `3` | Number of audit precedent recurrences required to promote an answer to cache. |

---

### 6. Agent & Execution Sandbox

| Variable | Default | Description |
| :--- | :--- | :--- |
| `MAX_AGENT_STEPS` | `4` | Maximum tool iteration steps allowed per query before budget exhaustion. |
| `SANDBOX_TIMEOUT_S` | `5.0` | Maximum execution time (seconds) allowed for sandboxed Python code. |
| `SANDBOX_MEMORY_MB` | `256` | OS-enforced commit memory limit (MB) for sandboxed subprocesses. |
| `SANDBOX_MAX_CONCURRENT` | `2` | Maximum concurrent sandboxed subprocesses allowed simultaneously. |
| `SANDBOX_NETWORK_MODE` | `none` | Network policy inside the sandbox (`none` or `best_effort`). |

---

### 7. Controlled Network & SSRF Guard

| Variable | Default | Description |
| :--- | :--- | :--- |
| `NETWORK_ENABLED` | `true` | Enables outbound network access for `web_search` and `http_fetch` tools. |
| `NETWORK_ALLOW_PRIVATE_IPS` | `false` | When false, SSRFGuard blocks loopback, RFC 1918 private subnets, link-local, and cloud metadata IPs. |
| `NETWORK_ALLOWED_DOMAINS` | `*` | Comma-separated domain allowlist (`*` allows all non-private domains). |
| `WEB_SEARCH_PROVIDER` | `duckduckgo` | Web search provider used by the `web_search` tool. |

---

### 8. Logging & Telemetry

| Variable | Default | Description |
| :--- | :--- | :--- |
| `LOG_LEVEL` | `INFO` | Console log verbosity (`DEBUG`, `INFO`, `WARNING`, `ERROR`). |

---

### 9. Langfuse Observability & Token Budget

| Variable | Default | Description |
| :--- | :--- | :--- |
| `LANGFUSE_PUBLIC_KEY` | *(unset)* | Langfuse project public key (`pk-lf-...`). |
| `LANGFUSE_SECRET_KEY` | *(unset)* | Langfuse project secret key (`sk-lf-...`). |
| `LANGFUSE_BASE_URL` | `https://cloud.langfuse.com` | Base URL for Langfuse Cloud or on-premise Docker instance. |
| `TOKEN_BUDGET` | `8192` | Target ceiling for prompt synthesis after extractive pruning. |
| `MAX_HISTORY_TOKENS` | `1500` | History token threshold before triggering abstractive turn summarization. |
| `MAX_OBSERVATION_TOKENS` | `6000` | Maximum tokens allocated to tool observations before relevance pruning. |

---

### 10. API Defense, Rate Limiter & Auth Security

| Variable | Default | Description |
| :--- | :--- | :--- |
| `RATE_LIMIT_ENABLED` | `true` | Enables in-memory sliding window rate limiting across API endpoints. |
| `MAX_PAYLOAD_SIZE_MB` | `10` | Maximum file upload size in MB (enforced by ASGI middleware $\to$ HTTP 413). |
| `MAX_JSON_SIZE_BYTES` | `1048576` | Maximum JSON request body size in bytes (1 MB $\to$ HTTP 413). |
| `AUTH_SESSION_TTL_S` | `86400.0` | Absolute session timeout in seconds (24 hours). |
| `AUTH_SESSION_IDLE_S` | `7200.0` | Inactivity/idle session timeout in seconds (2 hours). |

---

## Running & Integrating the SDK

### 1. Running the Standalone FastAPI Server
The SDK exports an enterprise FastAPI application with complete Swagger UI and ReDoc:

```bash
# Run server using the application factory
uvicorn nexus_engine.main:create_app --factory --host 127.0.0.1 --port 8766 --reload

# Interactive docs available at:
# http://127.0.0.1:8766/docs
# http://127.0.0.1:8766/openapi.json
```

### 2. Using the Python SDK Programmatically
Nexus Engine is designed for direct embedding into backend services and vertical applications:

```python
from nexus_engine import Settings, build_container

# 1. Load settings (from .env or explicit overrides)
settings = Settings.from_env()

# 2. Build dependency-injected container
container = build_container(settings)

# 3. Execute queries through the ReAct agent
response = container.agent.run(
    query="Review agreement termination obligations under section 4",
    session_id="user-session-001"
)

print(response.answer)
```

### 3. Running SDK Contract Tests
```bash
# Run SDK unit and contract verification tests
pytest tests
```

### 4. Reference Application & Full Benchmark Harness
For running the full-stack React 19 workbench UI, the full 530+ integration test suite, or the 125-vector automated evaluation benchmark rig (`python -m backend.evals.runner`), refer to the companion repository:
- **Repository**: [nexus-sample](https://github.com/TheWilsonGlobal/nexus-sample)
