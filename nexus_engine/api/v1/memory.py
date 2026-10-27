from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request

from nexus_engine.api.audit import audit_event
from nexus_engine.api.deps import (
    SECURITY_SESSION,
    authorize_session,
    get_container,
    is_elevated,
    rate_limit,
    require_roles,
)
from nexus_engine.core.container import Container
from nexus_engine.schemas.auth import AuthenticatedUser
from nexus_engine.schemas.memory import AuditChainStatus, AuditResponse, CreateSessionRequest, MemoryStats
from nexus_engine.services.security.validator import validate_identifier

router = APIRouter(prefix="/memory")


@router.get(
    "/stats",
    response_model=MemoryStats,
    dependencies=[Depends(rate_limit("general")), Depends(require_roles("admin", "operator", "auditor"))],
)
def stats(c: Container = Depends(get_container)) -> dict:
    return c.memory.stats()


@router.get(
    "/audit",
    response_model=AuditResponse,
    dependencies=[Depends(rate_limit("general"))],
)
@router.get(
    "/traces",
    response_model=AuditResponse,
    dependencies=[Depends(rate_limit("general"))],
)
def audit(
    session_id: Optional[str] = Query(default=None, max_length=64, pattern=r"^[A-Za-z0-9_-]+$"),
    limit: int = Query(default=50, ge=1, le=500),
    user: AuthenticatedUser = Depends(require_roles("admin", "operator", "auditor")),
    c: Container = Depends(get_container),
) -> dict:
    """Per-session audit requires ownership (admin/auditor override); the global listing, which
    includes the 'security' pseudo-session, is admin/auditor only."""
    if session_id:
        authorize_session(c, user, session_id)
    elif not is_elevated(user):
        raise HTTPException(status_code=403, detail="Access denied: global audit listing requires admin or auditor")
    return {"records": c.memory.audit.list_records(session_id=session_id or None, limit=limit)}


@router.get(
    "/audit/verify",
    response_model=AuditChainStatus,
    dependencies=[Depends(rate_limit("general")), Depends(require_roles("admin", "auditor"))],
)
@router.get(
    "/traces/verify",
    response_model=AuditChainStatus,
    dependencies=[Depends(rate_limit("general")), Depends(require_roles("admin", "auditor"))],
)
def audit_verify(c: Container = Depends(get_container)) -> dict:
    return c.memory.audit.verify_chain()

@router.get(
    "/sessions",
    dependencies=[Depends(rate_limit("general"))],
)
def get_sessions(
    user: AuthenticatedUser = Depends(require_roles("admin", "operator", "auditor")),
    c: Container = Depends(get_container),
) -> dict:
    return {"sessions": c.memory.list_sessions_for(user.username, is_elevated(user))}


@router.post(
    "/sessions",
    dependencies=[Depends(rate_limit("general"))],
)
def create_session(
    body: Optional[CreateSessionRequest] = Body(default=None),
    user: AuthenticatedUser = Depends(require_roles("admin", "operator")),
    c: Container = Depends(get_container),
) -> dict:
    """Create a session owned by the caller. A supplied ``session_id`` is validated and honoured;
    409 if it already belongs to someone else (idempotent for its owner)."""
    if body is not None and body.session_id:
        sid = validate_identifier(body.session_id, "session_id")
    else:
        sid = uuid.uuid4().hex[:12]
    if sid == SECURITY_SESSION:
        raise HTTPException(status_code=422, detail="session_id is reserved")
    owner = c.memory.claim_session(sid, user.username)
    if owner != user.username:
        raise HTTPException(status_code=409, detail="session_id already in use")
    return {
        "session_id": sid,
        "owner": owner,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


@router.delete(
    "/sessions/{session_id}",
    dependencies=[Depends(rate_limit("general"))],
)
def remove_session(
    session_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(require_roles("admin", "operator")),
    c: Container = Depends(get_container),
) -> dict:
    """404 when the session does not exist OR belongs to someone else (existence is not revealed
    to non-owners); admin may delete any session."""
    validate_identifier(session_id, "session_id")
    owner = c.memory.session_owner(session_id)
    if session_id == SECURITY_SESSION or not c.memory.session_exists(session_id):
        raise HTTPException(status_code=404, detail="session not found")
    if owner != user.username and "admin" not in user.roles:
        raise HTTPException(status_code=404, detail="session not found")
    c.memory.delete_session(session_id)
    audit_event(request, user, "memory.delete_session", f"session_id={session_id} owner={owner}")
    return {"session_id": session_id, "dropped": True}
