"""Who may run sandbox code, and whose code it may be.

Three independent rules, evaluated every time the tool is called (not once at startup):

1. the sandbox must be enabled (``SANDBOX_ENABLED``; off by default);
2. the caller must hold the ``admin`` role. This is a plain role check that does not depend on
   the enable flag, so turning the flag on never widens access beyond admins;
3. the code must come from the caller's own message. An LLM-chosen tool argument, or text that
   arrived through a tool observation (a fetched web page, a retrieved passage), is rejected
   unless it is literally contained in what the user typed. This is the code-level answer to
   indirect prompt injection (SEC-05): injected content cannot cause code to run.

The caller's identity travels in a ``ContextVar`` bound by the API layer around an agent run.
No bound principal means "not an authenticated request": refuse (fail closed).
"""
from __future__ import annotations

import contextlib
import re
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator, Optional, TypeVar

SANDBOX_ROLE = "admin"
_WS = re.compile(r"\s+")


@dataclass(frozen=True)
class Principal:
    username: str
    roles: frozenset[str]
    query: str  # the caller's own message for this turn
    user_code: Optional[str] = None  # code the planner extracted from that message, if any


_current: ContextVar[Optional[Principal]] = ContextVar("sandbox_principal", default=None)
T = TypeVar("T")


def current_principal() -> Optional[Principal]:
    return _current.get()


@contextlib.contextmanager
def bind_principal(principal: Principal) -> Iterator[None]:
    token = _current.set(principal)
    try:
        yield
    finally:
        _current.reset(token)


def iter_with_principal(principal: Principal, iterator: Iterator[T]) -> Iterator[T]:
    """Re-bind ``principal`` around every ``next()`` of ``iterator``.

    A streaming response pulls its generator through a thread pool, one ``next()`` per hop, and each
    hop runs in a copy of the caller's context; a ContextVar set once would be gone on the next hop.
    """
    while True:
        with bind_principal(principal):
            try:
                item = next(iterator)
            except StopIteration:
                return
        yield item


def _norm(text: str) -> str:
    return _WS.sub(" ", text).strip()


def code_comes_from_user(code: str, principal: Principal) -> bool:
    wanted = _norm(code)
    if not wanted:
        return False
    if principal.user_code is not None and _norm(principal.user_code) == wanted:
        return True
    return wanted in _norm(principal.query)


def refusal_reason(enabled: bool, code: str) -> Optional[str]:
    """None when the call may proceed, otherwise a short message safe to show to the model and the user."""
    if not enabled:
        return "sandbox disabled: code execution is turned off on this server (SANDBOX_ENABLED=false)"
    principal = current_principal()
    if principal is None:
        return "sandbox refused: no authenticated caller is bound to this request"
    if SANDBOX_ROLE not in principal.roles:
        return f"sandbox refused: running code requires the {SANDBOX_ROLE!r} role"
    if not code_comes_from_user(code, principal):
        return "sandbox refused: the code is not part of the user's own message"
    return None
