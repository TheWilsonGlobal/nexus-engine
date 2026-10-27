"""ASGI security defense middleware: HTTP security headers and payload size limit enforcement."""
from __future__ import annotations

import json
from typing import Any, Callable

from nexus_engine.services.security.validator import MAX_JSON_DEPTH, json_depth_exceeds

SECURITY_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"x-xss-protection", b"1; mode=block"),
    (b"referrer-policy", b"strict-origin-when-cross-origin"),
    (b"strict-transport-security", b"max-age=31536000; includeSubDomains"),
    (
        b"content-security-policy",
        b"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'",
    ),
)


# Swagger UI / ReDoc load their JS and CSS from a CDN and boot from an inline script, so the strict API CSP
# renders them as a blank page. These three read-only documentation paths are served WITHOUT a CSP header
# (the other security headers still apply); every API response keeps the strict policy.
CSP_EXEMPT_PATHS = frozenset({"/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"})
_CSP = b"content-security-policy"


def _host_without_port(raw: str) -> str:
    """'127.0.0.1:8766' -> '127.0.0.1', '[::1]:8766' -> '[::1]', 'localhost' -> 'localhost' (lower-cased)."""
    raw = raw.strip().lower()
    if raw.startswith("["):
        end = raw.find("]")
        return raw[: end + 1] if end != -1 else raw
    return raw.split(":", 1)[0]


def normalize_allowed_host(entry: str) -> str:
    """Config entries may be written bare ('::1') or bracketed ('[::1]'); compare in bracketed form."""
    entry = entry.strip().lower()
    if ":" in entry and not entry.startswith("["):
        return f"[{entry}]"
    return entry


class TrustedHostGuard:
    """Reject requests whose Host header is not an allowed name (400): defeats DNS rebinding.

    Starlette's TrustedHostMiddleware splits the header on the first ':', which mangles bracketed IPv6
    literals ('[::1]:8766'), so this guard parses the port off itself.
    """

    def __init__(self, app: Any, allowed_hosts: "list[str] | tuple[str, ...]") -> None:
        self.app = app
        self.allowed = frozenset(normalize_allowed_host(h) for h in allowed_hosts if h.strip())

    async def __call__(self, scope: dict[str, Any], receive: Callable[..., Any], send: Callable[..., Any]) -> None:
        if scope.get("type") not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        host = ""
        for k, v in scope.get("headers", []):
            if k.lower() == b"host":
                host = v.decode("latin1")
                break
        if _host_without_port(host) in self.allowed:
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        body = b'{"error":"invalid_host","detail":"Invalid host header"}'
        await send(
            {
                "type": "http.response.start",
                "status": 400,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode("latin1"))],
            }
        )
        await send({"type": "http.response.body", "body": body})


class _PayloadTooLargeError(Exception):
    def __init__(self, limit_bytes: int) -> None:
        super().__init__(f"Payload exceeds limit of {limit_bytes} bytes")
        self.limit_bytes = limit_bytes


class SecurityDefenseMiddleware:
    """Pure ASGI middleware enforcing request payload size caps (413) and response security headers."""

    def __init__(
        self,
        app: Any,
        max_json_bytes: int = 1_048_576,
        max_upload_bytes: int = 10 * 1_048_576,
        max_json_depth: int = MAX_JSON_DEPTH,
    ) -> None:
        self.app = app
        self.max_json_depth = max_json_depth
        self.max_json_bytes = max_json_bytes
        self.max_upload_bytes = max_upload_bytes

    async def __call__(self, scope: dict[str, Any], receive: Callable[..., Any], send: Callable[..., Any]) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        path: str = scope.get("path", "")
        is_upload = path.endswith("/ingestion/upload")
        max_bytes = self.max_upload_bytes if is_upload else self.max_json_bytes

        # 1. Check Content-Length header early before reading stream
        raw_headers: list[tuple[bytes, bytes]] = scope.get("headers", [])
        for k, v in raw_headers:
            if k.lower() == b"content-length":
                try:
                    content_len = int(v.decode("latin1").strip())
                except ValueError:
                    content_len = 0
                if content_len > max_bytes:
                    await self._send_413(send, max_bytes)
                    return
                break

        bytes_seen = 0
        response_started = False

        async def guarded_receive() -> dict[str, Any]:
            nonlocal bytes_seen
            message = await receive()
            if message.get("type") == "http.request":
                body = message.get("body", b"")
                bytes_seen += len(body)
                if bytes_seen > max_bytes:
                    raise _PayloadTooLargeError(max_bytes)
            return message

        async def guarded_send(message: dict[str, Any]) -> None:
            nonlocal response_started
            if message.get("type") == "http.response.start":
                response_started = True
                hdrs = list(message.get("headers", []))
                existing_keys = {k.lower() for k, _ in hdrs}
                for sk, sv in SECURITY_HEADERS:
                    if sk == _CSP and path in CSP_EXEMPT_PATHS:
                        continue
                    if sk not in existing_keys:
                        hdrs.append((sk, sv))
                state = scope.get("state") or {}
                rl_hdrs = state.get("rate_limit_headers") if isinstance(state, dict) else getattr(state, "rate_limit_headers", None)
                if rl_hdrs:
                    for rk, rv in rl_hdrs.items():
                        rk_bytes = rk.lower().encode("latin1")
                        if rk_bytes not in existing_keys:
                            hdrs.append((rk.encode("latin1"), str(rv).encode("latin1")))
                message = {**message, "headers": hdrs}
            await send(message)

        app_receive: Callable[..., Any] = guarded_receive
        try:
            if self._is_json_body_request(scope):
                # Bodies are capped at max_json_bytes, so buffering is bounded. Check nesting BEFORE any
                # handler parses it (a deep bomb would otherwise reach json.loads / pydantic).
                body = b""
                while True:
                    message = await guarded_receive()
                    if message.get("type") != "http.request":
                        break
                    body += message.get("body", b"")
                    if not message.get("more_body", False):
                        break
                if json_depth_exceeds(body, self.max_json_depth):
                    await self._send_422_depth(send, self.max_json_depth)
                    return
                replayed = False

                async def replay_receive() -> dict[str, Any]:
                    nonlocal replayed
                    if not replayed:
                        replayed = True
                        return {"type": "http.request", "body": body, "more_body": False}
                    return await receive()

                app_receive = replay_receive

            await self.app(scope, app_receive, guarded_send)
        except _PayloadTooLargeError:
            if not response_started:
                await self._send_413(send, max_bytes)
            else:
                raise

    @staticmethod
    def _is_json_body_request(scope: dict[str, Any]) -> bool:
        if scope.get("method", "GET").upper() not in ("POST", "PUT", "PATCH", "DELETE"):
            return False
        for k, v in scope.get("headers", []):
            if k.lower() == b"content-type":
                ctype = v.decode("latin1").split(";", 1)[0].strip().lower()
                return ctype == "application/json" or ctype.endswith("+json")
        return False

    @staticmethod
    async def _send_422_depth(send: Callable[..., Any], max_depth: int) -> None:
        payload = json.dumps(
            {"detail": f"JSON nesting depth exceeds maximum allowed limit ({max_depth})", "max_depth": max_depth}
        ).encode("utf-8")
        headers: list[tuple[bytes, bytes]] = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(payload)).encode("latin1")),
            *SECURITY_HEADERS,
        ]
        await send({"type": "http.response.start", "status": 422, "headers": headers})
        await send({"type": "http.response.body", "body": payload})

    @staticmethod
    async def _send_413(send: Callable[..., Any], max_bytes: int) -> None:
        payload = json.dumps(
            {
                "error": "payload_too_large",
                "detail": f"Request payload exceeds maximum allowed size of {max_bytes} bytes.",
                "max_bytes": max_bytes,
            }
        ).encode("utf-8")
        headers: list[tuple[bytes, bytes]] = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(payload)).encode("latin1")),
            *SECURITY_HEADERS,
        ]
        await send({"type": "http.response.start", "status": 413, "headers": headers})
        await send({"type": "http.response.body", "body": payload})
