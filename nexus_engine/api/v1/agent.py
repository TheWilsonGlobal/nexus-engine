from __future__ import annotations

import json
import logging
import re
from typing import Iterator, Optional

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from nexus_engine.api.deps import authorize_session, get_container, rate_limit, require_roles
from nexus_engine.api.errors import log_and_mask
from nexus_engine.core.container import Container
from nexus_engine.core.data_loader import load_examples
from nexus_engine.schemas.agent import AgentRunRequest, AgentRunResponse
from nexus_engine.schemas.auth import AuthenticatedUser
from nexus_engine.services.agent.planner import extract_code
from nexus_engine.services.sandbox.policy import Principal, bind_principal, iter_with_principal
from nexus_engine.services.security.validator import validate_identifier

log = logging.getLogger(__name__)
router = APIRouter(prefix="/agent")


@router.get(
    "/examples",
    dependencies=[Depends(rate_limit("general")), Depends(require_roles("admin", "operator", "auditor", "public_viewer"))],
)
def get_examples() -> list[dict[str, str]]:
    return load_examples()


def _principal(user: AuthenticatedUser, query: str) -> Principal:
    """The caller's identity for the sandbox policy: who they are and what they actually typed."""
    return Principal(user.username, frozenset(user.roles), query, extract_code(query))


def _authorize_requested_session(c: Container, user: AuthenticatedUser, session_id: Optional[str]) -> None:
    """403 unless ``user`` owns ``session_id`` (or is admin/auditor). An unowned id is claimed for ``user``."""
    if session_id:
        validate_identifier(session_id, "session_id")
        authorize_session(c, user, session_id, claim=True)


@router.post(
    "/run",
    response_model=AgentRunResponse,
    dependencies=[Depends(rate_limit("agent"))],
)
def run_agent(
    body: AgentRunRequest,
    c: Container = Depends(get_container),
    user: AuthenticatedUser = Depends(require_roles("admin", "operator")),
) -> AgentRunResponse:
    _authorize_requested_session(c, user, body.session_id)
    with bind_principal(_principal(user, body.query)):
        resp = c.agent.run(body.query, body.session_id)
    if not body.session_id:
        c.memory.claim_session(resp.session_id, user.username)  # the generated session belongs to its creator
    return resp


def _sse(event: str, data: str) -> str:
    return f"event: {event}\ndata: {data}\n\n"


@router.post(
    "/stream",
    dependencies=[Depends(rate_limit("agent"))],
)
def stream_agent(
    body: AgentRunRequest,
    c: Container = Depends(get_container),
    user: AuthenticatedUser = Depends(require_roles("admin", "operator")),
) -> StreamingResponse:
    _authorize_requested_session(c, user, body.session_id)
    principal = _principal(user, body.query)

    def gen() -> Iterator[str]:
        streamed = False  # did the LLM stream the answer's tokens live?
        try:
            for kind, payload in iter_with_principal(principal, c.agent.iter_run(body.query, body.session_id)):
                if kind == "step":
                    yield _sse("step", payload.model_dump_json())  # type: ignore[union-attr]
                elif kind == "token":
                    streamed = True
                    yield _sse("token", json.dumps({"text": payload}))
                elif kind == "reset":
                    streamed = False
                    yield _sse("reset", "{}")
                else:
                    if not body.session_id:
                        c.memory.claim_session(payload.session_id, user.username)  # type: ignore[union-attr]
                    # No live tokens (mock, cache hit, guardrail block): replay the answer word by word.
                    if not streamed:
                        for tok in re.findall(r"\S+\s*", payload.answer):  # type: ignore[union-attr]
                            yield _sse("token", json.dumps({"text": tok}))
                    yield _sse("done", payload.model_dump_json())  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001 - surface to the SSE client, without the exception text
            _cid, message = log_and_mask(log, "agent stream", exc)
            yield _sse("error", json.dumps({"detail": message}))

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
