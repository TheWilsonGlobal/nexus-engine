"""AST-based static gate for sandboxed code.

DEFENCE IN DEPTH ONLY. This is not the isolation: Python cannot be sandboxed by inspecting its
source (there is always another spelling), so the gate exists to refuse the obvious and the
obfuscated-but-common attempts *before* a process is spawned and to give the caller a clear
reason. The actual containment is the OS-level subprocess limits plus the in-child audit hook
(see ``executor.py`` and ``docs/sandbox.md``).

The gate is deliberately an allowlist for imports and a denylist for everything else:

* ``import`` / ``from ... import`` may only name a small set of pure-computation stdlib modules,
  so ``import os``, ``import importlib``, ``from os import system`` ... are all rejected, and a
  split spelling such as ``importlib.import_module('o' + 's')`` never gets that far because
  ``importlib`` is not importable and ``__import__`` is a denied name.
* Calls/names that reach the filesystem, the interpreter, or reflection are denied by name
  (``open``, ``eval``, ``exec``, ``compile``, ``__import__``, ``getattr`` ...).
* Any attribute access to a dunder (``x.__class__``, ``f.__globals__``) and to frame/generator
  introspection attributes is denied, as is any string constant containing ``__`` (which would
  otherwise reach attributes through ``str.format``).
"""
from __future__ import annotations

import ast
from typing import Optional

# Pure-computation modules. None of these opens files, sockets or processes on its own.
ALLOWED_IMPORTS = frozenset(
    {
        "math", "cmath", "json", "re", "statistics", "itertools", "functools", "collections",
        "decimal", "fractions", "datetime", "random", "string", "heapq", "bisect", "operator",
        "typing", "dataclasses", "textwrap", "numbers", "enum", "array", "copy", "time",
        "calendar", "hashlib", "base64", "binascii", "unicodedata", "difflib", "zlib",
    }
)

DENIED_NAMES = frozenset(
    {
        "open", "eval", "exec", "compile", "execfile", "__import__", "globals", "locals", "vars",
        "getattr", "setattr", "delattr", "breakpoint", "input", "help", "memoryview",
        "__builtins__", "__loader__", "__spec__", "builtins", "exit", "quit", "dir",
    }
)

# Attributes that are harmless in themselves but are the usual route out of a restricted namespace.
DENIED_ATTRS = frozenset(
    {
        "gi_frame", "gi_code", "gi_yieldfrom", "cr_frame", "cr_code", "cr_await", "ag_frame", "ag_code",
        "f_globals", "f_locals", "f_builtins", "f_back", "f_code", "tb_frame", "tb_next", "func_globals",
        "system", "popen", "startfile", "listdir", "scandir", "walk", "fork", "forkpty", "execv", "execl",
        "spawnv", "kill", "killpg", "environ", "getenv", "putenv", "remove", "unlink", "rmdir", "rmtree",
        "read_text", "read_bytes", "write_text", "write_bytes", "import_module", "load_module",
    }
)


def _is_dunder(name: str) -> bool:
    return len(name) > 4 and name.startswith("__") and name.endswith("__")


def check_code(code: str) -> Optional[str]:
    """Return a human-readable reason when ``code`` must not run, else ``None``.

    A syntax error is reported as a refusal too: the code could not run anyway and a parse
    failure here must never be read as "gate passed".
    """
    try:
        tree = ast.parse(code, mode="exec")
    except (SyntaxError, ValueError, RecursionError, MemoryError) as exc:
        return f"code does not parse: {type(exc).__name__}"

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top not in ALLOWED_IMPORTS:
                    return f"import of {alias.name!r} is not allowed"
        elif isinstance(node, ast.ImportFrom):
            top = (node.module or "").split(".")[0]
            if node.level or top not in ALLOWED_IMPORTS:
                return f"import from {('.' * node.level) + (node.module or '')!r} is not allowed"
        elif isinstance(node, ast.Name):
            if node.id in DENIED_NAMES:
                return f"use of {node.id!r} is not allowed"
            if _is_dunder(node.id) and node.id != "__name__":
                return f"use of dunder name {node.id!r} is not allowed"
        elif isinstance(node, ast.Attribute):
            if _is_dunder(node.attr):
                return f"dunder attribute access {node.attr!r} is not allowed"
            if node.attr in DENIED_ATTRS:
                return f"attribute {node.attr!r} is not allowed"
        elif isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)):
            text = node.value if isinstance(node.value, str) else node.value.decode("latin-1")
            if "__" in text and text != "__main__":
                return "string constants containing '__' are not allowed"
    return None
