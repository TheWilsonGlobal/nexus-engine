"""Input validation, identifier sanitization, path traversal guards, and JSON depth checks."""
from __future__ import annotations

import os
import re
from typing import Any

from fastapi import HTTPException

SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
ALLOWED_UPLOAD_EXTENSIONS = {".json", ".md", ".txt", ".pdf"}


def validate_identifier(value: str, field_name: str = "identifier") -> str:
    """Validate an identifier (such as doc_id or session_id) against path traversal and injection."""
    if not value or not isinstance(value, str):
        raise HTTPException(status_code=422, detail=f"Invalid {field_name}: must not be empty")
    cleaned = value.strip()
    if (
        "\x00" in cleaned
        or ".." in cleaned
        or "/" in cleaned
        or "\\" in cleaned
        or cleaned in (".", "..")
        or not SAFE_IDENTIFIER_RE.match(cleaned)
    ):
        raise HTTPException(
            status_code=422,
            detail=f"Invalid {field_name}: contains forbidden path traversal or special characters",
        )
    return cleaned


def sanitize_upload_filename(filename: str) -> str:
    """Validate and sanitize an uploaded filename against path traversal and disallowed extensions."""
    if not filename or "\x00" in filename:
        raise HTTPException(status_code=422, detail="Invalid filename: empty or contains null byte")

    if ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(
            status_code=422,
            detail="Invalid filename: path traversal sequences ('..', '/', '\\\\') are forbidden",
        )

    base = os.path.basename(filename).strip()
    if not base or base in (".", ".."):
        raise HTTPException(status_code=422, detail="Invalid filename")

    _, ext = os.path.splitext(base)
    if ext and ext.lower() not in ALLOWED_UPLOAD_EXTENSIONS:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported file extension '{ext}'. Allowed: {sorted(ALLOWED_UPLOAD_EXTENSIONS)}",
        )

    safe_base = re.sub(r"[^A-Za-z0-9_.-]+", "_", base).strip("._")
    if not safe_base:
        raise HTTPException(status_code=422, detail="Invalid filename after sanitization")
    return safe_base


MAX_JSON_DEPTH = 20


def json_depth_exceeds(data: bytes, max_depth: int = MAX_JSON_DEPTH) -> bool:
    """True if raw JSON text nests arrays/objects deeper than ``max_depth``.

    Single linear pass over the bytes, string/escape aware, no recursion and no parsing: safe to run on an
    attacker-controlled body BEFORE json.loads (which would raise RecursionError on a deep bomb).
    """
    depth = 0
    in_string = False
    escaped = False
    for b in data:
        if in_string:
            if escaped:
                escaped = False
            elif b == 0x5C:  # backslash
                escaped = True
            elif b == 0x22:  # closing quote
                in_string = False
            continue
        if b == 0x22:
            in_string = True
        elif b in (0x7B, 0x5B):  # { [
            depth += 1
            if depth > max_depth:
                return True
        elif b in (0x7D, 0x5D):  # } ]
            depth = max(depth - 1, 0)
    return False


def validate_json_depth(obj: Any, max_depth: int = MAX_JSON_DEPTH) -> None:
    """Reject deeply nested JSON values (422). Depth = number of nested arrays/objects; iterative."""
    stack: list[tuple[Any, int]] = [(obj, 1)]
    while stack:
        cur, depth = stack.pop()
        if isinstance(cur, dict):
            children: Any = cur.values()
        elif isinstance(cur, list):
            children = cur
        else:
            continue
        if depth > max_depth:
            raise HTTPException(
                status_code=422,
                detail=f"JSON nesting depth exceeds maximum allowed limit ({max_depth})",
            )
        stack.extend((child, depth + 1) for child in children)
