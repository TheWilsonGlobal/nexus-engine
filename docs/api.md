# API Reference

Nexus Engine serves a RESTful JSON API using `snake_case` keys, alongside real-time Server-Sent Events (SSE) streaming for agent execution, hardened by an ASGI security defense perimeter, sliding window rate limiter, and role-based access control.

Interactive OpenAPI documentation is served at `/docs` when the API server is running (`uvicorn nexus_engine.main:create_app --factory` at `http://127.0.0.1:8766/docs`), with the OpenAPI schema at `/openapi.json`.

---

## Endpoint Summary

### 1. Health & Status

| Method | Path | Description | Response Shape |
| :--- | :--- | :--- | :--- |
| `GET` | `/health` | Server health check (unauthenticated root) | `{"status": "ok", "version": str, "uptime_s": float}` |
| `GET` | `/api/v1/health` | Server health check under API prefix | `{"status": "ok", "version": str, "uptime_s": float}` |

---

### 2. Authentication & Session Management

| Method | Path | Description | Details |
| :--- | :--- | :--- | :--- |
| `POST` | `/api/v1/auth/login` | Authenticates username/password and creates a session | **Body:** `{"username": str(1..64), "password": str(1..128)}`<br>**Response:** `LoginResponse` (`token`, `user`, `expires_at`). Sets the `cogni_session` cookie: `HttpOnly`, `SameSite=Strict`, and `Secure` whenever `ENV` is not `development`. |
| `GET` | `/api/v1/auth/me` | Returns current authenticated user and session metadata | **Headers:** `Authorization: Bearer <token>` or cookie.<br>**Response:** `SessionInfo` (`session_id`, `user`, `expires_at`, `idle_expires_at`). |
| `POST` | `/api/v1/auth/logout` | Revokes the current session and clears cookies | **Response:** `{"ok": true, "message": "logged out"}`. |

---

### 3. Agent Execution & Reasoning Trace

| Method | Path | Description | Request / Response Details |
| :--- | :--- | :--- | :--- |
| `GET` | `/api/v1/agent/examples` | Sample query chips for UI | Returns `list[{"label": str, "query": str}]` |
| `POST` | `/api/v1/agent/run` | Synchronous agent execution | **Request:** `{"query": str(1..2000), "session_id": str|null}`<br>**Response:** `AgentRunResponse` (details below) |
| `POST` | `/api/v1/agent/stream` | Real-time SSE streaming of reasoning trace & tokens | **Request:** `{"query": str(1..2000), "session_id": str|null}`<br>**Events:** `step`, `token`, `reset`, `done`, `error` (see [agent.md §5](agent.md#5-real-time-streaming-protocol-sse)) |

#### `POST /api/v1/agent/run` Response Shape (`AgentRunResponse`):
```json
{
  "session_id": "0543ccd5",
  "answer": "Based on the available documentation...",
  "cache_hit": false,
  "blocked": false,
  "steps": [
    {
      "index": 0,
      "kind": "thought",
      "content": "Plan: factual question about the knowledge base. I will use search_knowledge_graph.",
      "tool": null,
      "tool_input": null,
      "ok": true,
      "duration_ms": 0.12
    },
    {
      "index": 1,
      "kind": "action",
      "content": "Calling search_knowledge_graph",
      "tool": "search_knowledge_graph",
      "tool_input": "What are the breach notification obligations of the processor?",
      "ok": true,
      "duration_ms": 0.0
    },
    {
      "index": 2,
      "kind": "observation",
      "content": "[Document: Global Export Control...] ...",
      "tool": "search_knowledge_graph",
      "tool_input": null,
      "ok": true,
      "duration_ms": 42.5
    },
    {
      "index": 3,
      "kind": "reflection",
      "content": "search_knowledge_graph returned usable output; no further tools needed.",
      "tool": null,
      "tool_input": null,
      "ok": true,
      "duration_ms": 0.05
    },
    {
      "index": 4,
      "kind": "final",
      "content": "Hello! I am Nexus Engine...",
      "tool": null,
      "tool_input": null,
      "ok": true,
      "duration_ms": 1120.3
    }
  ],
  "guardrail": {
    "risk": 0.0,
    "route": "fast_allow",
    "latency_ms": 0.45,
    "decision": "allow",
    "deep_verified": false
  },
  "sandbox": {
    "status": "idle",
    "duration_ms": null,
    "limits_enforced": {
      "timeout": true,
      "memory": true,
      "process_limit": true,
      "network_guard": "best_effort"
    }
  },
  "memory": {
    "hit_rate": 0.0
  },
  "prompt_tokens": 340,
  "completion_tokens": 96,
  "total_tokens": 436,
  "model": "gemini-3.5-flash-lite",
  "budget": {
    "raw_tokens": 12450,
    "compressed_tokens": 340,
    "compression_ratio": 0.9727,
    "budget_limit": 8192,
    "budget_exceeded": false,
    "tokens_saved_by_cache": 0,
    "estimated_cost_usd": 0.0000543,
    "cost_saved_usd": 0.0009082
  },
  "trace_id": "7b8f9a2e41c0"
}
```

#### SSE Stream Events (`POST /api/v1/agent/stream`):
- `event: step` — Emitted for each ReAct stage (`thought`, `action`, `observation`, `reflection`, `final`). Data is the JSON serialized `Step`.
- `event: token` — Emitted for each output token delta: `{"text": "token "}`.
- `event: reset` — Emitted when preliminary thinking streams are discarded upon tool call initiation.
- `event: done` — Emitted upon completion with the full `AgentRunResponse` payload.
- `event: error` — Emitted on unhandled exceptions: `{"detail": "ErrorType: message"}`.

---

### 4. Knowledge Ingestion & Document Management

| Method | Path | Description | Details |
| :--- | :--- | :--- | :--- |
| `POST` | `/api/v1/ingestion/upload` | Multipart file upload (`.pdf`, `.md`, `.txt`, `.json`) | Accepts `file`, optional `doc_id`, `format_hint`. Ingests into vector + graph storage. Requires `operator` or `admin` role. |
| `POST` | `/api/v1/ingestion/sample-pack` | Ingests built-in sample pack documents | Loads Commercial Contract Standards, Procurement SOP, Export Controls. |
| `POST` | `/api/v1/ingestion/reset` | Resets database and re-ingests clean sample pack | Clears vector/graph tables and re-initializes. Requires `admin` role. |
| `POST` | `/api/v1/ingestion/refactor/analyze` | Analyzes existing documents for semantic refactoring | Returns suggested splits/merges. |
| `POST` | `/api/v1/ingestion/refactor/apply` | Applies suggested refactoring changes | Updates chunk and graph topology. Requires `admin` role. |
| `GET` | `/api/v1/ingestion/documents` | Lists all ingested documents and metadata | Returns list of `DocumentMetaItem` (id, title, chunks, entities). |
| `GET` | `/api/v1/ingestion/documents/export` | Downloads the whole knowledge base as JSON (`knowledge-base-YYYYMMDD.json`) | `version`, `exported_at`, `stats`, `documents`, `chunks`, `graph{nodes,edges}`. Embeddings excluded. |
| `GET` | `/api/v1/ingestion/documents/{doc_id}/export` | Downloads one document rebuilt as Markdown (`{doc_id}.md`) | Leaf chunks in document order under headings. 404 if not found. |
| `DELETE` | `/api/v1/ingestion/documents/{doc_id}` | Deletes a document and its associated chunks/graph nodes | Cascading deletion. Requires `admin` role. |

---

### 5. Hybrid Retrieval & Knowledge Graph

| Method | Path | Description | Request / Response |
| :--- | :--- | :--- | :--- |
| `POST` | `/api/v1/retrieval/search` | Direct hybrid retrieval (Vector ANN + Graph Traversal) | **Body:** `{"query": str, "top_k": int(1..20, default=5)}`<br>**Response:** `{"results": [{"chunk_id": str, "text": str, "path": str, "score": float, "sources": ["dense", "graph"]}]}` |
| `GET` | `/api/v1/retrieval/graph` | Knowledge graph topology for visualization | Returns `{"nodes": [{"id": str, "label": str, "type": str}], "edges": [{"source": str, "target": str, "relation": str}]}` |
| `GET` | `/api/v1/retrieval/storage-stats` | Storage diagnostics | Chunk counts, embedding dimensions, graph node/edge counts. |
| `GET` | `/api/v1/retrieval/chunks` | Inspect raw indexed chunks | Paginated chunk listings. |

---

### 6. Memory & Audit Ledger

| Method | Path | Description | Details |
| :--- | :--- | :--- | :--- |
| `GET` | `/api/v1/memory/stats` | Memory tiers statistics | Hit/miss rates, LRU entries, promotion counters (`short_to_audit`, `verified_to_cache`, `audit_to_cache`). |
| `GET` | `/api/v1/memory/audit` | Query immutable audit log records | Query params: `session_id` (optional), `limit` (1..500, default 50). Ordered newest first. Requires `auditor` or `admin` role across all sessions. |
| `GET` | `/api/v1/memory/audit/verify` | Verify cryptographic hash-chain integrity | Returns `{"ok": bool, "records": int, "first_bad_id": int|null}`. Checks SHA-256 chain and triggers. |

---

### 7. Observability & Token Telemetry

| Method | Path | Description | Details |
| :--- | :--- | :--- | :--- |
| `GET` | `/api/v1/telemetry/tokens` | Cumulative token efficiency and cost metrics | Returns `CumulativeTokenMetrics` (`total_raw_tokens`, `total_compressed_tokens`, `avg_compression_ratio`, `total_tokens_saved_cache`, `total_cost_usd`, `total_cost_saved_usd`). |
| `POST` | `/api/v1/telemetry/tokens/reset` | Resets cumulative token telemetry counters | Clears running token totals and cost metrics. Requires `admin` role. |
| `GET` | `/api/v1/telemetry/traces` | Recent execution traces with granular span waterfalls | Query params: `limit` (1..100, default 20). Returns list of `TraceRecord` objects containing span breakdowns (`guardrail_evaluation`, `semantic_cache_check`, `tool_*`, `context_compression`, `llm_synthesis`). |

---

## Security Defense, Rate Limiting & Error Codes

### 1. HTTP Security Headers
Every response emitted by the application includes standard security headers:
- `X-Content-Type-Options: nosniff`
- `X-Frame-Options: DENY`
- `Strict-Transport-Security: max-age=63072000; includeSubDomains; preload`
- `Content-Security-Policy: default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'`
- `Referrer-Policy: strict-origin-when-cross-origin`

### 2. Sliding Window Rate Limiting
Endpoint tiers restrict burst traffic per client key (IP or user):
- **`auth` tier**: `5 requests / minute` (`/api/v1/auth/login`)
- **`agent` tier**: `15 requests / minute` (`/api/v1/agent/run`, `/api/v1/agent/stream`)
- **`ingestion` tier**: `10 requests / minute` (`/api/v1/ingestion/upload`)
- **`general` tier**: `120 requests / minute` (all other endpoints)

Headers emitted with every response:
- `X-RateLimit-Limit`: Maximum requests allowed in the window.
- `X-RateLimit-Remaining`: Remaining requests in the active window.
- `X-RateLimit-Reset`: Unix timestamp when the quota resets.

When quota is exceeded, the server returns **`HTTP 429 Too Many Requests`** with a **`Retry-After: <seconds>`** header.

### 3. Payload Size & Validation Limits
- **HTTP 413 Payload Too Large**:
  - Request body $> 1\text{ MB}$ (`1,048,576` bytes) for JSON requests.
  - File upload $> 10\text{ MB}$ for document ingestion.
- **HTTP 422 Unprocessable Content**:
  - JSON nesting recursion depth $> 20$ levels.
  - Path traversal characters in identifiers (`..`, `/`, `\`).
  - Disallowed file extensions during upload (`.exe`, `.sh`, `.py`).

### 4. Authentication Modes
- **Open Mode (Default)**: When `API_KEY` is unset in `.env`, the API operates in open mode for local development.
- **Enforced Mode**: When `API_KEY` is set, requests require:
  - `Authorization: Bearer <token_or_key>`
  - Or `X-API-Key: <key>`
  - Or `cogni_session` HttpOnly cookie.
  - Unauthenticated requests receive **`HTTP 401 Unauthorized`**.
  - Insufficient role authorization receives **`HTTP 403 Forbidden`** and logs a violation to the audit ledger.
