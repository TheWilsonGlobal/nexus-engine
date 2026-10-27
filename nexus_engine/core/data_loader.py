"""Data loader utility to load external mockup data assets with built-in fallbacks."""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Resolution paths for project data:
_REPO_ROOT = Path(__file__).resolve().parents[2]


def get_data_dir() -> Path:
    env_dir = os.environ.get("DATA_DIR") or os.environ.get("COGNI_DATA_DIR")
    if env_dir:
        p = Path(env_dir)
        if p.is_dir():
            return p
    cwd_data = Path.cwd() / "data"
    if (cwd_data / "corpus").is_dir():
        return cwd_data
    repo_data = _REPO_ROOT / "data"
    if repo_data.is_dir():
        return repo_data
    return cwd_data


_DEFAULT_DPA = """Document: Data Processing Agreement
Chapter I - General Provisions
Article 1 - Definitions
1. Personal Data means any information relating to an identified or identifiable Data Subject.
2. The Data Controller determines the purposes and means of processing, and the Data Processor processes Personal Data on its behalf.
Article 2 - Scope
1. This Agreement applies to all processing of Personal Data by the Data Processor for the Data Controller.
2. The Data Processor shall not engage a Sub-processor without prior written authorisation of the Data Controller.
Chapter II - Security and Breach
Article 3 - Security Measures
1. The Data Processor shall implement Encryption of Personal Data at rest and in transit.
2. Access Control shall follow least privilege and require Multi-factor Authentication for administrators.
3. Every access to Personal Data shall be written to an Audit Log retained for twelve months.
Article 4 - Breach Notification
1. The Data Processor shall notify the Data Controller of a Personal Data Breach without undue delay and within 48 hours.
2. The notification shall describe the nature of the breach, affected Data Subjects, and the Incident Response measures taken.
Chapter III - Term
Article 5 - Termination and Retention
1. Upon Termination the Data Processor shall delete or return all Personal Data at the choice of the Data Controller.
2. Retention of backup copies beyond thirty days is prohibited unless required by law.
3. Liability of each party is limited to the fees paid in the preceding twelve months, except for gross negligence.
"""

_DEFAULT_ESP = """Document: Enterprise Security Policy
Chapter I - Governance
Article 1 - Purpose
1. This policy defines security requirements for every Supplier and Customer environment.
2. Confidentiality of internal information must be preserved at all times.
Chapter II - Operations
Article 2 - Vulnerability Management
1. Each Vulnerability rated critical must be remediated within seven days.
2. Backup systems shall be tested quarterly and protected with Encryption.
Article 3 - Incident Response
1. An Incident Response team shall be on call and shall triage every security event within one hour.
2. Each incident shall be recorded in the Audit Log together with the remediation steps.
3. A Personal Data Breach must be escalated to the Data Controller contact immediately.
Article 4 - Access Management
1. Access Control reviews shall be performed every quarter.
2. Multi-factor Authentication is mandatory for all remote access.
"""

_DEFAULT_SAMPLE_DOCS: dict[str, str] = {
    "dpa": _DEFAULT_DPA,
    "esp": _DEFAULT_ESP,
}

_DEFAULT_EXAMPLES: list[dict[str, str]] = [
    {
        "id": "breach_notification",
        "label": "What are the breach notification obligations of the processor?",
        "query": "What are the breach notification obligations of the processor?",
        "category": "retrieval",
    },
    {
        "id": "calc",
        "label": "calculate 6*7",
        "query": "calculate 6*7",
        "category": "sandbox",
    },
    {
        "id": "hello",
        "label": "Hello",
        "query": "Hello",
        "category": "smalltalk",
    },
    {
        "id": "recall",
        "label": "Recall: what did I ask earlier?",
        "query": "Do you remember what I asked earlier?",
        "category": "memory",
    },
]

_FLAG_MAP: dict[str, int] = {
    "IGNORECASE": re.IGNORECASE,
    "DOTALL": re.DOTALL,
    "MULTILINE": re.MULTILINE,
}


def load_corpus() -> dict[str, str]:
    """Load corpus documents from data/corpus/ using manifest.json or text files."""
    data_dir = get_data_dir()
    corpus_dir = data_dir / "corpus"
    manifest_path = corpus_dir / "manifest.json"

    if manifest_path.is_file():
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest = json.load(f)
            docs: dict[str, str] = {}
            for item in manifest.get("documents", []):
                doc_id = item["id"]
                file_name = item.get("file", f"{doc_id}.txt")
                doc_path = corpus_dir / file_name
                if doc_path.is_file():
                    with open(doc_path, "r", encoding="utf-8") as df:
                        docs[doc_id] = df.read()
                else:
                    docs[doc_id] = _DEFAULT_SAMPLE_DOCS.get(doc_id, "")
            if docs:
                return docs
        except Exception as exc:
            log.warning("Failed to load corpus from %s: %s; using embedded defaults", manifest_path, exc)

    return dict(_DEFAULT_SAMPLE_DOCS)


def load_examples() -> list[dict[str, str]]:
    """Load example queries from data/examples.json."""
    data_dir = get_data_dir()
    examples_path = data_dir / "examples.json"
    if examples_path.is_file():
        try:
            with open(examples_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list) and data:
                return data
        except Exception as exc:
            log.warning("Failed to load examples from %s: %s; using embedded defaults", examples_path, exc)
    return list(_DEFAULT_EXAMPLES)


def load_guardrail_rules() -> list[tuple[str, re.Pattern[str], float]]:
    """Load guardrail patterns and weights from data/guardrails/rules.json."""
    data_dir = get_data_dir()
    rules_path = data_dir / "guardrails" / "rules.json"
    if rules_path.is_file():
        try:
            with open(rules_path, "r", encoding="utf-8") as f:
                raw_rules = json.load(f)
            rules: list[tuple[str, re.Pattern[str], float]] = []
            for r in raw_rules:
                name = r["name"]
                pattern_str = r["pattern"]
                weight = float(r["weight"])
                flags = 0
                for flag_name in r.get("flags", []):
                    flags |= _FLAG_MAP.get(flag_name, 0)
                compiled = re.compile(pattern_str, flags)
                rules.append((name, compiled, weight))
            if rules:
                return rules
        except Exception as exc:
            log.warning("Failed to load guardrail rules from %s: %s; using embedded defaults", rules_path, exc)

    # Fallback to standard fast filter rules
    flags = re.IGNORECASE | re.DOTALL
    return [
        (
            "instruction_override",
            re.compile(
                r"\b(ignore|disregard|forget|override)\b.{0,30}\b(previous|prior|above|all|earlier|your)\b"
                r".{0,30}\b(instruction|rule|prompt|guideline|polic)",
                flags,
            ),
            0.9,
        ),
        (
            "system_prompt_probe",
            re.compile(
                r"\b(reveal|show|print|leak|repeat|display)\b.{0,30}"
                r"\b(system prompt|hidden prompt|initial instructions|developer message)",
                flags,
            ),
            0.8,
        ),
        ("jailbreak", re.compile(r"\b(jailbreak|DAN mode|developer mode|do anything now)\b", flags), 0.85),
        (
            "exfiltration",
            re.compile(r"\b(exfiltrate|steal|dump)\b.{0,40}\b(credential|password|token|secret|database|data)", flags),
            0.8,
        ),
        ("malware", re.compile(r"\b(malware|ransomware|keylogger|reverse shell)\b", flags), 0.7),
        ("secrets", re.compile(r"\b(password|api[_ ]?key|private key|credential|secret)s?\b", flags), 0.4),
        (
            "destructive",
            re.compile(r"(rm\s+-rf|drop\s+table|format\s+c:|shutil\.rmtree|del\s+/f)", flags),
            0.6,
        ),
        ("code_exec", re.compile(r"(os\.system|subprocess|\beval\(|\bexec\(|__import__|ctypes)", flags), 0.5),
        ("network", re.compile(r"(socket\.|requests\.|urllib|\bcurl\b|\bwget\b)", flags), 0.4),
        ("encoded_blob", re.compile(r"[A-Za-z0-9+/=]{80,}"), 0.4),
    ]


def load_mock_templates() -> dict[str, Any]:
    """Load mock LLM prompt and response templates from data/prompts/mock_templates.json."""
    data_dir = get_data_dir()
    templates_path = data_dir / "prompts" / "mock_templates.json"
    if templates_path.is_file():
        try:
            with open(templates_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as exc:
            log.warning("Failed to load mock templates from %s: %s; using embedded defaults", templates_path, exc)

    return {
        "agent_loop": {
            "smalltalk_thought": "This is {talk} small talk, not a knowledge request; answering directly without tools.",
            "guardrail_block_thought": "Guardrail {route} (risk {risk:.2f}): request blocked; no tools will run.",
            "cache_hit_thought": "Semantic cache hit (similarity {similarity:.2f}, source {source}); returning cached answer without tools.",
            "budget_exhausted_reflection": "Step budget ({max_steps}) exhausted; stopping.",
            "action_calling": "Calling {tool}",
        },
        "think": {
            "default": "Plan: {rationale}. I will use {tool}.",
            "retry_prefix": "Retrying with a different approach. ",
        },
        "reflect": {
            "success": "{tool} returned usable output; no further tools needed.",
            "failure_switch": "{tool} failed ({output}). Switching to {next_tool}.",
            "failure_terminal": "{tool} failed ({output}) and no alternate tool remains.",
        },
        "synthesize": {
            "failure_unreliable": (
                "I could not answer this reliably. Attempts failed: {reasons}. "
                "I work best with questions about the knowledge base, short Python/arithmetic tasks, "
                "or recalling earlier turns. Try, for example: {examples}."
            ),
            "search_prefix": "Based on the knowledge base: ",
            "sandbox_prefix": "The sandbox executed your code successfully. Output: ",
            "recall_prefix": "From earlier interactions: ",
            "no_earlier_answer": (
                "This is the start of our conversation, so I have nothing earlier to recall. "
                "Ask me something and I can recall it afterwards."
            ),
        },
        "smalltalk": {
            "abilities": (
                "I can search the knowledge base (hybrid graph + vector retrieval), run small sandboxed "
                "Python snippets or arithmetic, and recall what you asked earlier in this session. "
                "Try, for example: {examples}."
            ),
            "thanks": "You're welcome! Ask me anything else whenever you like. ",
            "help": "Here is what I can do. ",
            "greeting": "Hello! ",
        },
        "refusal": "I can't help with that request: it was blocked by the safety guardrail ({reason}).",
    }
