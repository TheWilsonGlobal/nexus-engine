"""Enterprise Langfuse tracing and local latency waterfall telemetry."""
from __future__ import annotations

import collections
import datetime
import logging
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from nexus_engine.services.observability.config import ObservabilityConfig

log = logging.getLogger(__name__)

ACTIVE_TRACE_TTL_S = 900.0  # a trace never ended within this long is considered abandoned


class SpanRecord(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:8])
    name: str
    duration_ms: float = 0.0
    input: Optional[Any] = None
    output: Optional[Any] = None
    status: str = "ok"


class TraceRecord(BaseModel):
    id: str
    name: str
    session_id: str
    created_at: str
    total_latency_ms: float = 0.0
    spans: List[SpanRecord] = Field(default_factory=list)
    tokens: Dict[str, Any] = Field(default_factory=dict)
    cost_usd: float = 0.0
    status: str = "ok"


class ActiveSpan:
    def __init__(self, name: str, tracer: "ObservabilityTracer", trace_id: str, lf_span: Any = None) -> None:
        self.name = name
        self.tracer = tracer
        self.trace_id = trace_id
        self.lf_span = lf_span
        self.start_t = time.perf_counter()
        self.input_data: Optional[Any] = None

    def end(self, output: Optional[Any] = None, status: str = "ok") -> None:
        dur = round((time.perf_counter() - self.start_t) * 1000.0, 2)
        rec = SpanRecord(
            name=self.name,
            duration_ms=dur,
            input=self.input_data,
            output=output,
            status=status,
        )
        self.tracer._record_span(self.trace_id, rec)
        if self.lf_span:
            try:
                self.lf_span.end(output=output)
            except Exception as e:
                log.warning("Langfuse span end error: %s", e)


class ObservabilityTracer:
    """Non-blocking Langfuse observability tracer with in-memory buffer for local UI."""

    def __init__(
        self,
        config: Optional[ObservabilityConfig] = None,
        max_history: int = 50,
        client: Any = None,
        active_ttl_s: float = ACTIVE_TRACE_TTL_S,
    ) -> None:
        self.config = config or ObservabilityConfig.from_env()
        self.client = None
        self.disabled_reason: Optional[str] = None
        self.active_ttl_s = active_ttl_s
        self._lock = threading.Lock()
        self._recent_traces: collections.deque[TraceRecord] = collections.deque(maxlen=max_history)
        self._active_traces: Dict[str, Dict[str, Any]] = {}

        if client is not None:  # injected (tests / custom wiring)
            self._adopt_client(client)
        elif self.config.enabled:
            try:
                from langfuse import Langfuse
            except Exception as e:
                self._disable(f"langfuse SDK not importable: {e}")
                return
            if not hasattr(Langfuse, "trace"):
                # Checked on the class, before instantiating: no connection is attempted for an unusable SDK.
                self._disable("unsupported langfuse version (this tracer needs the v2 API: Langfuse.trace)")
                return
            try:
                self._adopt_client(
                    Langfuse(
                        public_key=self.config.public_key,
                        secret_key=self.config.secret_key,
                        host=self.config.base_url,
                    )
                )
                log.info("Langfuse tracing initialized: host=%s", self.config.base_url)
            except Exception as e:
                self._disable(f"failed to initialize Langfuse SDK: {e}")

    def _adopt_client(self, client: Any) -> None:
        if not callable(getattr(client, "trace", None)):
            self._disable("unsupported langfuse version (client has no trace())")
            return
        self.client = client

    def _disable(self, reason: str) -> None:
        self.client = None
        self.disabled_reason = reason
        # One loud warning at construction; local (in-memory) traces keep working.
        log.warning("tracing disabled: %s", reason)

    def start_trace(self, name: str, session_id: str, query: str) -> str:
        trace_id = uuid.uuid4().hex
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        lf_trace = None

        if self.client:
            try:
                lf_trace = self.client.trace(
                    id=trace_id,
                    name=name,
                    session_id=session_id,
                    input={"query": query},
                )
            except Exception as e:
                log.warning("Langfuse trace start error: %s", e)

        self._sweep_stale()
        with self._lock:
            self._active_traces[trace_id] = {
                "name": name,
                "session_id": session_id,
                "created_at": now_iso,
                "start_t": time.perf_counter(),
                "spans": [],
                "lf_trace": lf_trace,
            }
        return trace_id

    def span(self, trace_id: str, name: str, input_data: Optional[Any] = None) -> ActiveSpan:
        lf_span = None
        with self._lock:
            active = self._active_traces.get(trace_id)
            if active and active.get("lf_trace"):
                try:
                    lf_span = active["lf_trace"].span(name=name, input=input_data)
                except Exception as e:
                    log.warning("Langfuse span start error: %s", e)

        span_obj = ActiveSpan(name=name, tracer=self, trace_id=trace_id, lf_span=lf_span)
        span_obj.input_data = input_data
        return span_obj

    def _record_span(self, trace_id: str, span_rec: SpanRecord) -> None:
        with self._lock:
            active = self._active_traces.get(trace_id)
            if active:
                active["spans"].append(span_rec)

    def end_trace(
        self,
        trace_id: str,
        output: Optional[Any] = None,
        tokens: Optional[Dict[str, Any]] = None,
        cost_usd: float = 0.0,
        status: str = "ok",
    ) -> Optional[TraceRecord]:
        with self._lock:
            active = self._active_traces.pop(trace_id, None)

        if not active:
            return None

        return self._finalize(trace_id, active, output, tokens, cost_usd, status)

    def _finalize(
        self,
        trace_id: str,
        active: Dict[str, Any],
        output: Optional[Any],
        tokens: Optional[Dict[str, Any]],
        cost_usd: float,
        status: str,
    ) -> TraceRecord:
        dur = round((time.perf_counter() - active["start_t"]) * 1000.0, 2)
        record = TraceRecord(
            id=trace_id,
            name=active["name"],
            session_id=active["session_id"],
            created_at=active["created_at"],
            total_latency_ms=dur,
            spans=active["spans"],
            tokens=tokens or {},
            cost_usd=cost_usd,
            status=status,
        )

        with self._lock:
            self._recent_traces.appendleft(record)

        if active.get("lf_trace"):
            try:
                active["lf_trace"].update(output=output)
                if self.client:
                    self.client.flush()
            except Exception as e:
                log.warning("Langfuse trace end error: %s", e)

        return record

    def _sweep_stale(self) -> int:
        """Finalize traces whose owner never called end_trace (crashed/abandoned) so the dict cannot grow."""
        cutoff = time.perf_counter() - self.active_ttl_s
        with self._lock:
            stale = [tid for tid, a in self._active_traces.items() if a["start_t"] < cutoff]
            popped = [(tid, self._active_traces.pop(tid)) for tid in stale]
        for tid, active in popped:
            log.warning("Trace %s never ended within %.0fs; closing as abandoned", tid, self.active_ttl_s)
            self._finalize(tid, active, None, None, 0.0, "abandoned")
        return len(popped)

    def active_count(self) -> int:
        with self._lock:
            return len(self._active_traces)

    def get_recent_traces(self, limit: int = 20) -> List[TraceRecord]:
        with self._lock:
            return list(self._recent_traces)[:limit]
