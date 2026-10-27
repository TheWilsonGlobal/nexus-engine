"""Tier 1: sliding-window working memory (K turns + token truncation)."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass


@dataclass
class Turn:
    role: str
    content: str
    tokens: int


def count_tokens(text: str) -> int:
    """Whitespace token estimate (mock tokenizer)."""
    return len(text.split())


class ShortTermMemory:
    """Holds at most ``max_turns`` turns and ``max_tokens`` tokens.

    ``add`` returns the turns that were evicted so the manager can promote them
    to the persistent tier instead of losing them. A single turn longer than
    ``max_tokens`` is truncated (the truncated tail is not kept).
    """

    def __init__(self, max_turns: int = 8, max_tokens: int = 512) -> None:
        if max_turns < 1 or max_tokens < 1:
            raise ValueError("max_turns and max_tokens must be >= 1")
        self.max_turns = max_turns
        self.max_tokens = max_tokens
        self._turns: deque[Turn] = deque()

    def add(self, role: str, content: str) -> list[Turn]:
        words = content.split()
        if len(words) > self.max_tokens:
            content = " ".join(words[: self.max_tokens])
        self._turns.append(Turn(role, content, count_tokens(content)))
        evicted: list[Turn] = []
        while len(self._turns) > self.max_turns or (
            self.tokens > self.max_tokens and len(self._turns) > 1
        ):
            evicted.append(self._turns.popleft())
        return evicted

    @property
    def tokens(self) -> int:
        return sum(t.tokens for t in self._turns)

    def __len__(self) -> int:
        return len(self._turns)

    def turns(self) -> list[Turn]:
        return list(self._turns)

    def context(self) -> str:
        return "\n".join(f"{t.role}: {t.content}" for t in self._turns)
