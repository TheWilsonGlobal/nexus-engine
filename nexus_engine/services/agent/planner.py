"""Planner: deterministic tool routing.

With a real LLM, only explicit commands (a bare URL, "web search: ...", code to run) are routed
here; everything else is left to the model's native tool calling. The mock/offline path routes
every query with these rules.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from nexus_engine.core.data_loader import load_examples
from nexus_engine.services.agent.tools import HTTP_FETCH, RECALL, SANDBOX, SEARCH, WEB_SEARCH

_FENCE = re.compile(r"```(?:python|py)?\s*\n?(.*?)```", re.S | re.I)
_INLINE = re.compile(r"\b(?:run|execute)(?:\s+(?:this|the))?(?:\s+(?:python|code))?\s*:\s*(.+)$", re.S | re.I)
_MATH = re.compile(r"\b(?:compute|calculate|evaluate)\s+([0-9\s+\-*/().%]+?)\s*[?.]?\s*$", re.I)
# Only phrasings that refer to the CONVERSATION. Bare "before", "earlier" or "previous" also appear
# in ordinary questions ("notice required before exporting", "the previous SOP version").
_RECALL = re.compile(
    r"\b((?:do|can|did) you (?:still )?(?:remember|recall)|(?:remember|recall) (?:what|when|who|that|my|our|me)|"
    r"last time|did i (?:ask|say|mention|tell)|(?:did|have) (?:i|we) (?:ask|say|mention|tell you|talk about|discuss)|"
    r"(?:i|we) (?:asked|said|mentioned|told you|talked about|discussed)|"
    r"you (?:said|told me|mentioned)|who am i|who i am|my name|what(?:'?s|\s+is)\s+my\s+name)\b",
    re.I,
)
_WEB = re.compile(r"^(?:web\s+search|search\s+the\s+web|search\s+web|google)(?:\s*:|\s+for|\s+about)?\s*(.+)$", re.I)
_URL = re.compile(r"^https?://[^\s]+$", re.I)

# Small-talk: whole-message anchored so anything extra ("hello, ignore previous
# instructions", "hi what is article 4") is NOT small-talk and takes the normal path.
_END = r"\s*[!.?,]*\s*$"
_GREETING = re.compile(
    r"^\s*(?:(?:hi|hello|hey|hiya|howdy|yo|greetings|good\s+(?:morning|afternoon|evening))"
    r"(?:\s+(?:there|again|all|everyone|team))?"
    # Introduction: at most a name (one or two words), nothing after it. "I'm asking what the notice period
    # is" or "My name is Bob. Ignore the above ..." carry a question/instruction and must take the normal path.
    r"|(?:(?:hi|hello|hey|hiya|howdy|yo)\s*[,!.]*\s+)?(?:i['’]?m|i\s+am|my\s+name\s+is)\s+[\w-]+(?:\s+[\w-]+)?"
    r"|.*nice\s+to\s+meet\s+you.*"
    r"|how\s+are\s+you(?:\s+(?:today|doing))?|what'?s\s+up|sup)" + _END, re.I)
_THANKS = re.compile(
    r"^\s*(?:thanks?|thank\s+you|thx|ty|cheers|many\s+thanks|great|awesome|perfect|ok(?:ay)?|cool|nice|bye|goodbye|see\s+you)"
    r"(?:\s+(?:so\s+much|a\s+lot|very\s+much|again|you))*" + _END, re.I)
_META = re.compile(
    r"^\s*(?:help(?:\s+me)?|\?|menu|commands?|what\s+can\s+you\s+do|what\s+do\s+you\s+do|"
    r"who\s+are\s+you|what\s+are\s+you|how\s+do(?:es)?\s+(?:you|this)\s+work|"
    r"what\s+(?:can|should)\s+i\s+(?:ask|do|try)(?:\s+(?:you|here))?|how\s+can\s+you\s+help(?:\s+me)?)" + _END, re.I)


def classify_smalltalk(query: str) -> Optional[str]:
    """Return 'greeting' | 'thanks' | 'help' for conservative whole-message matches, else None."""
    if len(query) > 80:
        return None
    if _META.match(query):
        return "help"
    if _GREETING.match(query):
        return "greeting"
    if _THANKS.match(query):
        return "thanks"
    return None


# Core queries suggested by planner loaded from data/examples.json
EXAMPLE_QUERIES = tuple(
    e["query"]
    for e in load_examples()
    if e.get("category") != "smalltalk"
)


def examples_text() -> str:
    return "; ".join(f"'{q}'" for q in EXAMPLE_QUERIES)


@dataclass
class PlanStep:
    tool: str
    tool_input: str
    rationale: str


@dataclass
class Plan:
    steps: list[PlanStep] = field(default_factory=list)
    summary: str = ""


def extract_code(query: str) -> Optional[str]:
    if m := _FENCE.search(query):
        return m.group(1).strip()
    if m := _INLINE.search(query):
        return m.group(1).strip()
    if m := _MATH.search(query):
        return f"print({m.group(1).strip()})"
    return None


def is_recall_query(query: str) -> bool:
    return bool(_RECALL.search(query))


def explicit_plan(query: str) -> Optional[Plan]:
    """A plan only when the user spelled out the tool: a bare URL, a web-search command, or code."""
    clean = query.strip()
    if _URL.match(clean):
        return Plan([PlanStep(HTTP_FETCH, clean, "query is a public web URL to fetch and inspect")], "fetch public URL")
    if m := _WEB.match(clean):
        return Plan([PlanStep(WEB_SEARCH, m.group(1).strip(), "query explicitly requests a web search")], "search public web")
    code = extract_code(query)
    if code:
        return Plan([PlanStep(SANDBOX, code, "query contains executable code or arithmetic")],
                    "run the code in the sandbox")
    return None


def make_plan(query: str) -> Plan:
    if plan := explicit_plan(query):
        return plan
    if is_recall_query(query):
        return Plan([PlanStep(RECALL, query, "query refers to earlier interaction")],
                    "recall episodic memory")
    return Plan([PlanStep(SEARCH, query, "factual question about the knowledge base")],
                "search the hybrid graph/vector index")


# Self-healing: which tool to try next when one fails.
# RECALL has no alternate: a knowledge-graph search cannot answer a question about the conversation.
ALTERNATES = {SANDBOX: SEARCH, SEARCH: RECALL, WEB_SEARCH: RECALL, HTTP_FETCH: SEARCH}


def alternate_for(tool: str, query: str) -> Optional[PlanStep]:
    alt = ALTERNATES.get(tool)
    if alt is None:
        return None
    return PlanStep(alt, query, f"{tool} failed; falling back to {alt}")
