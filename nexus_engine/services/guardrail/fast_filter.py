"""Fast loop: precompiled pattern/heuristic safety scorer (no model, sub-ms)."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from nexus_engine.core.data_loader import load_guardrail_rules

FLAGS = re.IGNORECASE | re.DOTALL

# (name, pattern, weight) loaded from data/guardrails/rules.json with fallback
_RULES: list[tuple[str, re.Pattern[str], float]] = load_guardrail_rules()



def scan_signals(text: str) -> list[tuple[str, float]]:
    return [(name, w) for name, rx, w in _RULES if rx.search(text)]


def combine(weights: list[float]) -> float:
    """Noisy-OR: independent signals reinforce each other, result stays in [0, 1]."""
    p = 1.0
    for w in weights:
        p *= 1.0 - w
    return round(min(1.0, max(0.0, 1.0 - p)), 4)


@dataclass
class FastResult:
    risk: float
    latency_ms: float
    signals: list[str] = field(default_factory=list)


def fast_filter(text: str) -> FastResult:
    t0 = time.perf_counter()
    sig = scan_signals(text)
    risk = combine([w for _, w in sig])
    return FastResult(risk, (time.perf_counter() - t0) * 1000.0, [n for n, _ in sig])


# Signals that describe an attempt to steer the model. Legitimate documents may mention malware, secrets
# or shell commands, so tool output is only blocked for these instruction-shaped signals.
INJECTION_SIGNALS = frozenset({"instruction_override", "jailbreak", "system_prompt_probe", "exfiltration"})


def scan_untrusted_output(text: str) -> list[str]:
    """Names of injection signals found in a tool result (empty = clean). Used before output reaches the LLM."""
    return sorted({name for name, _ in scan_signals(text)} & INJECTION_SIGNALS)
