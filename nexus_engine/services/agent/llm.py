"""Deterministic mock LLM driven by external templates (data/prompts/mock_templates.json)."""
from __future__ import annotations

from typing import Any, Optional

from nexus_engine.core.data_loader import load_mock_templates
from nexus_engine.services.agent.planner import PlanStep, examples_text
from nexus_engine.services.agent.tools import RECALL, SANDBOX, SEARCH, ToolResult

_TEMPLATES: dict[str, Any] = load_mock_templates()

NO_EARLIER_ANSWER = _TEMPLATES["synthesize"]["no_earlier_answer"]


class MockLLM:
    def __init__(self, templates: Optional[dict[str, Any]] = None) -> None:
        self.templates = templates or _TEMPLATES

    def loop_step(self, key: str, **kwargs: Any) -> str:
        tpl = self.templates.get("agent_loop", {})
        template = tpl.get(key, "")
        return template.format(**kwargs)

    def smalltalk_thought(self, talk: str) -> str:
        return self.loop_step("smalltalk_thought", talk=talk)

    def guardrail_thought(self, route: str, risk: float) -> str:
        return self.loop_step("guardrail_block_thought", route=route, risk=risk)

    def cache_hit_thought(self, similarity: float, source: str) -> str:
        return self.loop_step("cache_hit_thought", similarity=similarity, source=source)

    def budget_exhausted_reflection(self, max_steps: int) -> str:
        return self.loop_step("budget_exhausted_reflection", max_steps=max_steps)

    def action_calling(self, tool: str) -> str:
        return self.loop_step("action_calling", tool=tool)

    def think(self, query: str, step: PlanStep, attempt: int) -> str:
        tpl = self.templates["think"]
        prefix = tpl["retry_prefix"] if attempt > 1 else ""
        text = tpl["default"].format(rationale=step.rationale, tool=step.tool)
        return f"{prefix}{text}"

    def reflect(self, tool: str, result: ToolResult, next_step: Optional[PlanStep]) -> str:
        tpl = self.templates["reflect"]
        if result.ok:
            return tpl["success"].format(tool=tool)
        if next_step is not None:
            return tpl["failure_switch"].format(tool=tool, output=result.output, next_tool=next_step.tool)
        return tpl["failure_terminal"].format(tool=tool, output=result.output)

    def synthesize(self, query: str, good: list[tuple[str, ToolResult]], failed: list[tuple[str, ToolResult]]) -> str:
        syn = self.templates["synthesize"]
        if not good:
            reasons = "; ".join(f"{t}: {r.output}" for t, r in failed) or "no tool could be applied"
            return syn["failure_unreliable"].format(reasons=reasons, examples=examples_text())
        parts: list[str] = []
        for tool, res in good:
            if tool == SEARCH:
                top = res.citations[:2]
                parts.append(syn["search_prefix"] + " | ".join(top))
            elif tool == SANDBOX:
                parts.append(f"{syn['sandbox_prefix']}{res.output}")
            elif tool == RECALL and res.empty:
                parts.append(syn["no_earlier_answer"])
            elif tool == RECALL:
                parts.append(syn["recall_prefix"] + res.output.replace("\n", " | "))
        return " ".join(parts)

    @staticmethod
    def smalltalk(kind: str) -> str:
        st = _TEMPLATES["smalltalk"]
        abilities = st["abilities"].format(examples=examples_text())
        if kind == "thanks":
            return st["thanks"] + abilities
        if kind == "help":
            return st["help"] + abilities
        return st["greeting"] + abilities

    @staticmethod
    def refusal(reason: str) -> str:
        return _TEMPLATES["refusal"].format(reason=reason)
