import json
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Request

from nexus_engine.api.deps import get_container, rate_limit, require_roles
from nexus_engine.core.constants import DEFAULT_LLM_LOCAL_MODEL
from nexus_engine.core.container import Container

router = APIRouter()


def _safe_err(e: BaseException) -> str:
    """Probe failures are reported by class (and HTTP status), never by raw exception text, which can carry
    URLs, hostnames or resolver details."""
    if isinstance(e, urllib.error.HTTPError):
        return f"HTTP {e.code}"
    return type(e).__name__


def _probe_embedding_status(
    provider: str,
    url: str,
    gemini_key: str | None = None,
    model: str = "text-embedding-004",
    timeout_s: float = 1.5,
) -> dict[str, Any]:
    if provider == "gemini":
        gemini_ok = False
        gemini_code = None
        gemini_err = None
        if not gemini_key:
            gemini_err = "GEMINI_EMBEDDING_API_KEY not configured"
        else:
            try:
                m = "gemini-embedding-001" if ("004" in model or "embedding-001" in model) else model
                p_url = f"https://generativelanguage.googleapis.com/v1beta/models/{m}:embedContent"
                body = json.dumps({"content": {"parts": [{"text": "ping"}]}}).encode("utf-8")
                req = urllib.request.Request(
                    p_url,
                    method="POST",
                    headers={"Content-Type": "application/json", "x-goog-api-key": gemini_key},
                    data=body,
                )
                with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                    gemini_ok = True
                    gemini_code = resp.status
            except urllib.error.HTTPError as e:
                gemini_code = e.code
                gemini_err = _safe_err(e)
            except Exception as e:
                gemini_err = _safe_err(e)

        if gemini_ok:
            return {
                "online": True,
                "code": gemini_code,
                "provider": "gemini",
                "model": model,
                "fallback_provider": "local_server",
                "fallback_model": "multilingual-e5-base",
            }

        # If Gemini fails, probe local_server fallback
        local_ok = False
        try:
            req = urllib.request.Request(url, method="POST", headers={"Content-Type": "application/json"}, data=b'{"model":"test","input":[]}')
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                local_ok = True
        except Exception:
            pass

        return {
            "online": local_ok,
            "provider": "local_server (fallback active)" if local_ok else "gemini",
            "model": "multilingual-e5-base" if local_ok else model,
            "error": None if local_ok else f"Gemini offline ({gemini_err}); local_server fallback unreachable",
            "fallback_provider": "local_server",
            "fallback_model": "multilingual-e5-base",
        }
    elif provider == "mock":
        return {"online": True, "code": 200, "provider": "mock", "model": "mock-crc32"}
    else:
        try:
            req = urllib.request.Request(url, method="POST", headers={"Content-Type": "application/json"}, data=b'{"model":"test","input":[]}')
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                return {"online": True, "code": resp.status, "provider": "remote"}
        except urllib.error.HTTPError as e:
            return {"online": True, "code": e.code, "provider": "remote"}
        except Exception as e:
            return {"online": False, "error": _safe_err(e), "provider": "remote"}


def _probe_llm_status(
    provider: str,
    gemini_key: str | None = None,
    model: str = "gemini-3.5-flash-lite",
    local_url: str | None = None,
    local_model: str = DEFAULT_LLM_LOCAL_MODEL,
    timeout_s: float = 1.5,
) -> dict[str, Any]:
    if provider == "gemini":
        if not gemini_key:
            return {"online": False, "provider": "gemini", "model": model, "error": "GEMINI_LLM_API_KEY not configured"}
        try:
            url = "https://generativelanguage.googleapis.com/v1beta/models"
            req = urllib.request.Request(url, method="GET", headers={"x-goog-api-key": gemini_key})
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                return {
                    "online": True,
                    "code": resp.status,
                    "provider": "gemini",
                    "model": model,
                    "fallback_provider": "local_server",
                    "fallback_model": local_model,
                }
        except urllib.error.HTTPError as e:
            return {
                "online": False,
                "code": e.code,
                "provider": "gemini",
                "model": model,
                "error": _safe_err(e),
                "fallback_provider": "local_server",
                "fallback_model": local_model,
            }
        except Exception as e:
            return {
                "online": False,
                "provider": "gemini",
                "model": model,
                "error": _safe_err(e),
                "fallback_provider": "local_server",
                "fallback_model": local_model,
            }
    elif provider == "mock":
        return {"online": True, "code": 200, "provider": "mock", "model": model}
    else:
        url = local_url or "http://127.0.0.1:11434/v1"
        try:
            req = urllib.request.Request(f"{url}/models", method="GET")
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                return {"online": True, "code": resp.status, "provider": provider, "model": local_model}
        except Exception as e:
            return {"online": False, "error": _safe_err(e), "provider": provider, "model": local_model}


PROBE_CACHE_TTL_S = 30.0


class ProbeCache:
    """Caches the outbound provider probes so health checks cannot be used to hammer upstreams."""

    def __init__(self, ttl_s: float = PROBE_CACHE_TTL_S, clock: Callable[[], float] = time.monotonic) -> None:
        self.ttl_s = ttl_s
        self._clock = clock
        self._lock = threading.Lock()
        self._value: Optional[dict[str, Any]] = None
        self._at = 0.0

    def get(self, produce: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        with self._lock:  # held across produce() so concurrent callers share one probe round
            now = self._clock()
            if self._value is None or now - self._at >= self.ttl_s:
                self._value = produce()
                self._at = self._clock()
            return self._value


def _probe_cache(request: Request) -> ProbeCache:
    cache = getattr(request.app.state, "health_probe_cache", None)
    if cache is None:
        cache = request.app.state.health_probe_cache = ProbeCache()
    return cache


def _safe_url(url: str) -> str:
    """scheme://host[:port] only: no credentials, path or query."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{host}{port}" if parts.scheme and host else ""


def _db_info(name: str, kind: str, path: str) -> dict[str, Any]:
    exists = os.path.exists(path)
    return {
        "name": name,
        "file": os.path.basename(path),
        "type": kind,
        "exists": exists,
        "size_bytes": os.path.getsize(path) if exists else 0,
    }


@router.get("/health")
def health() -> dict:
    """Unauthenticated liveness only. Nothing about configuration or dependencies."""
    return {"status": "ok"}


@router.get(
    "/health/details",
    dependencies=[Depends(rate_limit("general")), Depends(require_roles("admin", "auditor"))],
)
def health_details(request: Request, c: Container = Depends(get_container)) -> dict:
    """Authenticated (admin/auditor) diagnostics. No secrets, no absolute paths; provider probes are
    cached for PROBE_CACHE_TTL_S seconds."""

    def produce() -> dict[str, Any]:
        return {
            "embedding_probe": _probe_embedding_status(
                provider=c.settings.embedding_provider,
                url=c.settings.embedding_url,
                gemini_key=c.settings.gemini_embedding_api_key or c.settings.gemini_api_key,
                model=c.settings.embedding_model,
            ),
            "llm_probe": _probe_llm_status(
                provider=c.settings.llm_provider,
                gemini_key=c.settings.gemini_api_key,
                model=c.settings.llm_model,
                local_url=c.settings.llm_local_url,
                local_model=c.settings.llm_local_model,
            ),
        }

    probes = _probe_cache(request).get(produce)
    return {
        "status": "ok",
        "version": c.settings.version,
        "uptime_s": round(time.monotonic() - c.started_at, 3),
        "settings": {
            "llm_provider": c.settings.llm_provider,
            "llm_model": c.settings.llm_model,
            "gemini_model_name": c.settings.gemini_model_name,
            "llm_temperature": c.settings.llm_temperature,
            "llm_local_url": _safe_url(c.settings.llm_local_url),
            "llm_local_model": c.settings.llm_local_model,
            "embedding_provider": c.settings.embedding_provider,
            "embedding_model": c.settings.embedding_model,
            "embedding_url": _safe_url(c.settings.embedding_url),
            "embedding_dim": c.settings.embedding_dim,
            "network_enabled": c.settings.network_enabled,
            "ssrf_enforce": not c.settings.network_allow_private_ips,
            "sandbox_timeout_s": c.settings.sandbox_timeout_s,
            "sandbox_memory_mb": c.settings.sandbox_memory_mb,
            "sandbox_max_concurrent": c.settings.sandbox_max_concurrent,
            "max_agent_steps": c.settings.max_agent_steps,
            "cache_threshold": c.settings.cache_threshold,
            "cache_max_entries": c.settings.cache_max_entries,
            "short_term_max_turns": c.settings.short_term_max_turns,
            "max_sessions": c.settings.max_sessions,
        },
        "databases": [
            _db_info("Nexus Primary DB", "SQLite (WAL + CTE)", c.settings.database_path),
            _db_info("Audit & Episodic Journal", "SQLite (Append-Only SHA-256)", c.settings.audit_db_path),
        ],
        **probes,
    }
