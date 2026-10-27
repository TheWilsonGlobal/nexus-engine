"""Application factory.

Nothing is built at import time (no database opened, no tracer initialised). Run with:
    uvicorn nexus_engine.main:create_app --factory
"""
from __future__ import annotations

from typing import Optional

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, Response

from nexus_engine.api.deps import require_api_key
from nexus_engine.api.v1 import agent, auth, health, ingestion, memory, retrieval, telemetry
from nexus_engine.core.config import Settings, _env
from nexus_engine.core.container import build_container
from nexus_engine.core.logging import setup_logging
from nexus_engine.core.startup_checks import is_development, run_startup_checks
from nexus_engine.services.security.middleware import SecurityDefenseMiddleware, TrustedHostGuard


# Request methods / headers the SPA and API clients actually use (preflight is answered for these only).
CORS_ALLOW_METHODS = ["GET", "POST", "DELETE", "OPTIONS"]
CORS_ALLOW_HEADERS = ["Authorization", "Content-Type", "Accept", "X-API-Key"]
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def allowed_hosts(settings: Settings) -> list[str]:
    """Host header values the server answers to (DNS-rebinding defence).

    Loopback names always; the configured bind host; ``testserver`` only in development (Starlette's
    TestClient); plus any comma-separated names in ALLOWED_HOSTS (e.g. ``backend`` behind docker compose,
    or the public hostname of a deployment).
    """
    hosts = list(LOOPBACK_HOSTS)
    if settings.backend_host not in ("0.0.0.0", "::", ""):
        hosts.append(settings.backend_host)
    if is_development():
        hosts.append("testserver")
    extra = _env("ALLOWED_HOSTS") or ""
    hosts.extend(h.strip() for h in extra.split(",") if h.strip() and h.strip() != "*")
    return hosts


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or Settings.from_env()
    setup_logging(settings.log_level)
    run_startup_checks(settings)  # refuses unsafe auth config before any service is built
    app = FastAPI(title="nexus-engine", version=settings.version)
    app.state.container = build_container(settings)
    app.add_middleware(
        SecurityDefenseMiddleware,
        max_json_bytes=settings.max_json_size_bytes,
        max_upload_bytes=settings.max_payload_size_mb * 1024 * 1024,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_methods=CORS_ALLOW_METHODS,
        allow_headers=CORS_ALLOW_HEADERS,
        expose_headers=[
            "X-RateLimit-Limit",
            "X-RateLimit-Remaining",
            "X-RateLimit-Reset",
            "Retry-After",
        ],
    )
    # Added last = outermost: a bad Host is refused before CORS, auth, or any handler runs.
    app.add_middleware(TrustedHostGuard, allowed_hosts=allowed_hosts(settings))
    app.include_router(health.router)  # GET /health
    app.include_router(health.router, prefix="/api/v1")  # GET /api/v1/health
    app.include_router(auth.router, prefix="/api/v1")  # Auth endpoints (/api/v1/auth)
    # Everything except /health sits behind the optional API key (no-op when COGNI_API_KEY is unset).
    for r in (agent.router, memory.router, retrieval.router, ingestion.router, telemetry.router):
        app.include_router(r, prefix="/api/v1", dependencies=[Depends(require_api_key)])

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        # The backend has no UI of its own; send browsers to the interactive API docs.
        return RedirectResponse(url="/docs")

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> Response:
        return Response(status_code=204)

    return app


_app: Optional[FastAPI] = None


def get_app() -> FastAPI:
    """Process-wide app, built on first call from the environment (for code that needs a shared instance)."""
    global _app
    if _app is None:
        _app = create_app()
    return _app


if __name__ == "__main__":
    import uvicorn

    cfg = Settings.from_env()
    uvicorn.run(
        "nexus_engine.main:create_app", factory=True, host=cfg.backend_host, port=cfg.backend_port, reload=True
    )
