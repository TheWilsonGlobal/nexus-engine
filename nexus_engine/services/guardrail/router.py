"""Single routing entry point for the dual-process guardrail."""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Callable, Literal, Optional

from nexus_engine.services.guardrail.deep_verifier import DeepVerifier
from nexus_engine.services.guardrail.fast_filter import FastResult, fast_filter

LOW = 0.35
HIGH = 0.75

Route = Literal["fast_allow", "deep_verify", "fast_block"]


def route_for_risk(risk: float) -> Route:
    """risk < 0.35 -> allow; 0.35 <= risk <= 0.75 -> deep verify; risk > 0.75 -> block."""
    if risk < LOW:
        return "fast_allow"
    if risk <= HIGH:
        return "deep_verify"
    return "fast_block"


@dataclass
class GuardrailDecision:
    risk: float
    route: Route
    latency_ms: float  # fast-stage latency (the part with the <20ms budget)
    decision: Literal["allow", "block"]
    deep_verified: bool = False
    reason: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("reason")
        return d


class Guardrail:
    def __init__(
        self,
        verifier: Optional[DeepVerifier] = None,
        scorer: Callable[[str], FastResult] = fast_filter,
    ) -> None:
        self.verifier = verifier or DeepVerifier()
        self.scorer = scorer

    def evaluate(self, text: str, risk_override: Optional[float] = None) -> GuardrailDecision:
        """Score ``text`` (or use an injected ``risk_override``) and route it."""
        t0 = time.perf_counter()
        if risk_override is None:
            risk = self.scorer(text).risk
        else:
            if not 0.0 <= risk_override <= 1.0:
                raise ValueError("risk_override must be in [0, 1]")
            risk = risk_override
        latency = (time.perf_counter() - t0) * 1000.0
        route = route_for_risk(risk)
        if route == "fast_allow":
            return GuardrailDecision(risk, route, latency, "allow", False, "low risk")
        if route == "fast_block":
            return GuardrailDecision(risk, route, latency, "block", False, "high risk")
        verdict = self.verifier.verify(text)
        return GuardrailDecision(
            risk, route, latency, "allow" if verdict.allow else "block", True, verdict.reason
        )
