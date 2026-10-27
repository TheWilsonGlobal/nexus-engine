"""FastAPI dependencies: container, unified authentication, RBAC guards, and rate limiting."""
from __future__ import annotations

import hmac
import logging
from typing import Callable, Optional

from fastapi import Cookie, Depends, Header, HTTPException, Request, Response

from nexus_engine.core.container import Container
from nexus_engine.schemas.auth import AuthenticatedUser

log = logging.getLogger(__name__)


def get_container(request: Request) -> Container:
    return request.app.state.container


def is_loopback(request: Request) -> bool:
    host = request.client.host if request.client else ""
    return host in ("127.0.0.1", "::1", "localhost")


def get_client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def get_current_user(
    request: Request,
    x_api_key: Optional[str] = Header(default=None),
    authorization: Optional[str] = Header(default=None),
    cogni_session: Optional[str] = Cookie(default=None),
) -> AuthenticatedUser:
    """Unified authentication resolver supporting Session Tokens (Bearer/Cookie), API Keys, and Local Dev Mode."""
    c: Container = request.app.state.container
    bearer_token: Optional[str] = None
    if authorization and authorization[:7].lower() == "bearer ":
        bearer_token = authorization[7:].strip()

    # 1. Check active user session (Bearer token takes precedence over cookie)
    for candidate in (bearer_token, cogni_session):
        if candidate:
            sess = c.auth_mgr.get_session(candidate)
            if sess is not None:
                user = AuthenticatedUser(
                    username=sess.username,
                    roles=list(sess.roles),
                    auth_method="session",
                )
                request.state.user = user
                return user

    # 2. Check configured shared API key
    expected_key = c.settings.api_key
    supplied_key = x_api_key or bearer_token
    if expected_key:
        if supplied_key and hmac.compare_digest(supplied_key.encode("utf-8"), expected_key.encode("utf-8")):
            user = AuthenticatedUser(
                username="api_key_client",
                roles=["admin"],
                auth_method="api_key",
            )
            request.state.user = user
            return user
        raise HTTPException(
            status_code=401,
            detail="invalid or missing API key",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 3. No valid credentials. There is no anonymous identity and no client-based exemption
    #    (User-Agent, peer address): the caller must present a session token or the API key.
    raise HTTPException(
        status_code=401,
        detail="Authentication required",
        headers={"WWW-Authenticate": "Bearer"},
    )


ELEVATED_ROLES = frozenset({"admin", "auditor"})
SECURITY_SESSION = "security"


def is_elevated(user: AuthenticatedUser) -> bool:
    """admin and auditor may read every session; everyone else only their own."""
    return any(role in ELEVATED_ROLES for role in user.roles)


def authorize_session(c: Container, user: AuthenticatedUser, session_id: str, *, claim: bool = False) -> None:
    """Enforce session ownership; raises 403 when ``user`` may not touch ``session_id``.

    * the 'security' pseudo-session is elevated-only;
    * an owned session is open to its owner and to elevated users;
    * an unowned session (legacy rows written before ownership existed, or an unknown id) is
      elevated-only, except that with ``claim=True`` (run / create) it is bound to ``user``.
    """
    if session_id == SECURITY_SESSION:
        if not is_elevated(user):
            raise HTTPException(status_code=403, detail="Access denied: session not owned by caller")
        return
    elevated = is_elevated(user)
    owner = c.memory.session_owner(session_id)
    if owner is None and claim:
        owner = c.memory.claim_session(session_id, user.username)
    if owner == user.username or elevated:
        return
    raise HTTPException(status_code=403, detail="Access denied: session not owned by caller")


def require_api_key(user: AuthenticatedUser = Depends(get_current_user)) -> AuthenticatedUser:
    """Router-level authentication guard (backwards-compatible alias for get_current_user)."""
    return user


def require_roles(*allowed_roles: str) -> Callable[..., AuthenticatedUser]:
    """RBAC dependency factory enforcing that the authenticated user holds at least one allowed role."""

    def _role_guard(
        request: Request,
        user: AuthenticatedUser = Depends(get_current_user),
    ) -> AuthenticatedUser:
        if not any(role in allowed_roles for role in user.roles):
            c: Container = request.app.state.container
            log.warning(
                "RBAC violation: user=%s roles=%s path=%s required=%s",
                user.username,
                user.roles,
                request.url.path,
                list(allowed_roles),
            )
            c.memory.log(
                "security",
                "security",
                f"denied user={user.username} roles={user.roles} path={request.url.path} required={list(allowed_roles)}",
                tool="rbac_denied",
            )
            raise HTTPException(
                status_code=403,
                detail=f"Access denied: requires one of roles {list(allowed_roles)}",
            )
        return user

    return _role_guard


def rate_limit(tier: str = "general") -> Callable[..., None]:
    """Rate-limiting dependency factory enforcing sliding-window limits per client and endpoint tier."""

    def _limiter(request: Request, response: Response) -> None:
        c: Container = request.app.state.container
        client_ip = get_client_ip(request)
        res = c.rate_limiter.check(client_ip, tier=tier)
        hdrs = c.rate_limiter.headers(res)
        request.state.rate_limit_headers = hdrs
        for k, v in hdrs.items():
            response.headers[k] = v
        if not res.allowed:
            log.warning("Rate limit exceeded: ip=%s tier=%s limit=%d", client_ip, tier, res.limit)
            raise HTTPException(
                status_code=429,
                detail={
                    "error": "rate_limit_exceeded",
                    "detail": f"Rate limit exceeded for tier '{tier}'. Retry after {res.reset_after_s}s.",
                    "retry_after": res.reset_after_s,
                },
                headers=hdrs,
            )

    return _limiter
