"""Uniform, non-leaking error responses: details go to the server log, the client gets a correlation id."""
from __future__ import annotations

import logging
import uuid

from fastapi import HTTPException

GENERIC_ERROR = "Internal error"


def new_correlation_id() -> str:
    return uuid.uuid4().hex[:12]


def log_and_mask(log: logging.Logger, what: str, exc: BaseException) -> tuple[str, str]:
    """Log ``exc`` with a fresh correlation id; return (id, client-safe message). No exception text leaves the server."""
    cid = new_correlation_id()
    log.error("[%s] %s: %s", cid, what, exc, exc_info=exc)
    return cid, f"{what} failed. Reference: {cid}"


def internal_error(log: logging.Logger, what: str, exc: BaseException) -> HTTPException:
    """A 500 whose detail names the operation and a correlation id, never the exception text."""
    _cid, message = log_and_mask(log, what, exc)
    return HTTPException(status_code=500, detail=message)
