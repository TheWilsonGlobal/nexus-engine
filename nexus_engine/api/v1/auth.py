"""Authentication endpoints: login, session introspection (/me), and logout."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Request, Response

from nexus_engine.api.audit import audit_event, hash_identifier
from nexus_engine.api.deps import get_client_ip, get_container, rate_limit
from nexus_engine.core.container import Container
from nexus_engine.core.startup_checks import is_development
from nexus_engine.schemas.auth import AuthenticatedUser
from nexus_engine.schemas.auth import LoginRequest, LoginResponse, SessionInfo

router = APIRouter(prefix="/auth", tags=["auth"])


def _extract_session_token(
    authorization: Optional[str],
    cogni_session: Optional[str],
) -> Optional[str]:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    if cogni_session:
        return cogni_session
    return None


@router.post("/login", response_model=LoginResponse, dependencies=[Depends(rate_limit("auth"))])
def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    c: Container = Depends(get_container),
) -> LoginResponse:
    user = c.auth_mgr.authenticate(body.username, body.password)
    if user is None:
        # The username field is attacker-controlled and is sometimes a mistyped password: never store it
        # raw. A short hash still lets an operator correlate repeated attempts against the same name.
        c.memory.log(
            "security", "security",
            f"login_failed user_hash={hash_identifier(body.username)} ip={get_client_ip(request)}",
            tool="auth_login",
        )
        raise HTTPException(status_code=401, detail="Invalid username or password")

    sess = c.auth_mgr.create_session(user.username, user.roles)
    c.memory.log("security", "security", f"login_success username={user.username} roles={user.roles}", tool="auth_login")

    response.set_cookie(
        key="cogni_session",
        value=sess.token,
        httponly=True,
        secure=not is_development(),  # Secure outside ENV=development (browsers still accept it on localhost)
        samesite="strict",  # the SPA reaches the API same-origin through its /api proxy; no cross-site flows
        path="/",
        max_age=int(c.settings.auth_session_ttl_s),
    )

    return LoginResponse(
        username=sess.username,
        roles=sess.roles,
        token=sess.token,
    )


@router.get("/me", response_model=SessionInfo)
def me(
    authorization: Optional[str] = Header(default=None),
    cogni_session: Optional[str] = Cookie(default=None),
    c: Container = Depends(get_container),
) -> SessionInfo:
    token = _extract_session_token(authorization, cogni_session)
    if not token:
        raise HTTPException(status_code=401, detail="Unauthorized")

    sess = c.auth_mgr.get_session(token)
    if sess is None:
        raise HTTPException(status_code=401, detail="Unauthorized")

    return SessionInfo(
        username=sess.username,
        roles=sess.roles,
    )


@router.post("/logout")
def logout(
    request: Request,
    response: Response,
    authorization: Optional[str] = Header(default=None),
    cogni_session: Optional[str] = Cookie(default=None),
    c: Container = Depends(get_container),
) -> dict[str, bool]:
    token = _extract_session_token(authorization, cogni_session)
    if token:
        sess = c.auth_mgr.get_session(token)
        c.auth_mgr.revoke_session(token)
        if sess is not None:
            audit_event(request, AuthenticatedUser(username=sess.username, roles=list(sess.roles), auth_method="session"), "auth.logout", sess.username)

    response.delete_cookie(key="cogni_session", path="/")
    return {"ok": True}
