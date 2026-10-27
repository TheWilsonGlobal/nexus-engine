"""Audit trail for privileged and destructive API actions.

Rows land in the tamper-evident audit log (hash chain) under the ``security`` pseudo-session, which only
admin/auditor roles may read. Every row records WHO (authenticated username), FROM WHERE (client IP) and
WHAT (action + target), so a destructive call can always be attributed.
"""
from __future__ import annotations

import hashlib
import re
from typing import Optional

from fastapi import Request

from nexus_engine.api.deps import SECURITY_SESSION, get_client_ip
from nexus_engine.schemas.auth import AuthenticatedUser

AUDIT_TOOL = "audit_event"
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")
MAX_FIELD = 120


def clean_field(value: object) -> str:
    """Single-line, length-capped rendering so a hostile id/filename cannot forge extra audit fields."""
    text = _CONTROL.sub(" ", str(value)).strip()
    return text[:MAX_FIELD]


def hash_identifier(value: str) -> str:
    """Short, non-reversible tag for an UNTRUSTED identifier (e.g. a username typed into a login form).

    Failed-login input may be a mistyped password; it must never be stored. The tag still lets an operator
    correlate repeated attempts against the same name.
    """
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:12]


def audit_event(
    request: Request,
    user: Optional[AuthenticatedUser],
    action: str,
    target: object = "",
    *,
    outcome: str = "ok",
) -> int:
    """Append one audit row and return its id. ``user`` is None only for pre-authentication events."""
    c = request.app.state.container
    who = clean_field(user.username) if user is not None else "anonymous"
    roles = ",".join(clean_field(r) for r in user.roles) if user is not None else "-"
    content = (
        f"audit action={clean_field(action)} outcome={clean_field(outcome)} user={who} roles={roles} "
        f"ip={clean_field(get_client_ip(request))} target={clean_field(target)}"
    )
    return c.memory.log(SECURITY_SESSION, "security", content, tool=AUDIT_TOOL)
