"""Telemetry, token efficiency, and observability API router."""
from __future__ import annotations

from typing import List
from fastapi import APIRouter, Depends, Query, Request

from nexus_engine.api.audit import audit_event
from nexus_engine.api.deps import get_container, get_current_user, is_elevated, rate_limit, require_roles
from nexus_engine.core.container import Container
from nexus_engine.schemas.auth import AuthenticatedUser
from nexus_engine.services.agent.budget import CumulativeTokenMetrics
from nexus_engine.services.observability.tracer import TraceRecord

router = APIRouter(prefix="/telemetry", tags=["telemetry"])


@router.get(
    "/tokens",
    response_model=CumulativeTokenMetrics,
    dependencies=[Depends(rate_limit("general")), Depends(require_roles("admin", "operator", "auditor"))],
)
def get_token_metrics(c: Container = Depends(get_container)) -> CumulativeTokenMetrics:
    """Return cumulative token usage, compression ratio, and cost savings."""
    return c.budget_mgr.get_metrics()


@router.post(
    "/tokens/reset",
    response_model=CumulativeTokenMetrics,
    dependencies=[Depends(rate_limit("general")), Depends(require_roles("admin"))],
)
def reset_token_metrics(
    request: Request,
    c: Container = Depends(get_container),
    user: AuthenticatedUser = Depends(get_current_user),
) -> CumulativeTokenMetrics:
    """Reset cumulative token metrics."""
    c.budget_mgr.reset_metrics()
    audit_event(request, user, "telemetry.tokens_reset", "cumulative token metrics")
    return c.budget_mgr.get_metrics()


@router.get(
    "/traces",
    response_model=List[TraceRecord],
    dependencies=[Depends(rate_limit("general")), Depends(require_roles("admin", "operator", "auditor"))],
)
def get_recent_traces(
    limit: int = Query(default=20, ge=1, le=100),
    user: AuthenticatedUser = Depends(require_roles("admin", "operator", "auditor")),
    c: Container = Depends(get_container),
) -> List[TraceRecord]:
    """Return recent execution traces with granular span waterfalls.

    admin/auditor see every trace; other users only traces of sessions they own."""
    if is_elevated(user):
        return c.tracer.get_recent_traces(limit=limit)
    owners = c.memory.audit.session_owners()
    everything = c.tracer.get_recent_traces(limit=1_000_000)
    return [t for t in everything if owners.get(t.session_id) == user.username][:limit]
