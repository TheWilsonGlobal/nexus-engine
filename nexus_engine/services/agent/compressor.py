"""Extractive context compressor and token pruning engine (120k -> 8k tokens)."""
from __future__ import annotations

import re
from typing import List, Tuple
from nexus_engine.services.agent.budget import estimate_tokens

_WORD_RE = re.compile(r"\w+")


def _extract_keywords(text: str) -> set[str]:
    """Extract lowercase search keywords, filtering basic stop words."""
    words = _WORD_RE.findall(text.lower())
    stop_words = {"the", "a", "an", "is", "are", "was", "were", "and", "or", "to", "in", "of", "for", "with", "on", "at", "by"}
    return {w for w in words if len(w) > 2 and w not in stop_words}


class ContextCompressor:
    """Dynamically condenses massive tool observations and history into an 8k token budget."""

    def __init__(
        self,
        max_budget: int = 8192,
        max_history_tokens: int = 1500,
        max_observation_tokens: int = 6000,
    ) -> None:
        self.max_budget = max_budget
        self.max_history_tokens = max_history_tokens
        self.max_observation_tokens = max_observation_tokens

    def compress_history(self, history_text: str, query: str) -> str:
        """Condense dialogue history when it exceeds allocation."""
        if not history_text.strip():
            return ""

        cur_tokens = estimate_tokens(history_text)
        if cur_tokens <= self.max_history_tokens:
            return history_text

        # Dialogue turns are typically separated by double newlines or role labels
        lines = [line.strip() for line in history_text.split("\n") if line.strip()]
        if len(lines) <= 4:
            # If few lines but long, truncate older lines
            target_chars = self.max_history_tokens * 3
            return f"[Context Truncated] ... {history_text[-target_chars:]}"

        # Keep last 3 turns verbatim, summarize earlier turns
        recent_lines = lines[-3:]
        earlier_lines = lines[:-3]

        # Extract salient entities/topics from earlier lines
        earlier_text = " ".join(earlier_lines)
        keywords = _extract_keywords(earlier_text)
        sample_topics = ", ".join(list(keywords)[:8]) if keywords else "prior context"

        summary = f"[Summary of {len(earlier_lines)} prior turns: discussed {sample_topics}]"
        return f"{summary}\n" + "\n".join(recent_lines)

    def compress_observations(self, observations_text: str, query: str) -> str:
        """Score and prune observation paragraphs based on query relevance."""
        if not observations_text.strip():
            return ""

        cur_tokens = estimate_tokens(observations_text)
        if cur_tokens <= self.max_observation_tokens:
            return observations_text

        query_keywords = _extract_keywords(query)
        # Split into distinct paragraphs or chunks
        raw_paras = [p.strip() for p in re.split(r"\n\s*\n", observations_text) if p.strip()]
        if not raw_paras:
            raw_paras = [observations_text]

        # Score each paragraph: keyword overlap density + position bonus
        scored_paras: List[Tuple[float, int, str]] = []
        for idx, para in enumerate(raw_paras):
            para_words = _extract_keywords(para)
            overlap = len(query_keywords.intersection(para_words)) if query_keywords else 1
            # Add small freshness/sequence weight
            score = float(overlap * 10) + (1.0 / (idx + 1))
            scored_paras.append((score, idx, para))

        # Sort by score descending to pick highest information density
        scored_paras.sort(key=lambda x: x[0], reverse=True)

        selected: List[Tuple[int, str]] = []
        accumulated_tokens = 0
        budget = self.max_observation_tokens

        for _score, original_idx, para in scored_paras:
            p_tokens = estimate_tokens(para)
            if accumulated_tokens + p_tokens <= budget:
                selected.append((original_idx, para))
                accumulated_tokens += p_tokens
            elif accumulated_tokens < budget:
                # Take partial paragraph to fill remaining budget
                remaining_tokens = budget - accumulated_tokens
                allowed_chars = int(remaining_tokens * 3.5)
                truncated_para = para[:allowed_chars] + "... [pruned]"
                selected.append((original_idx, truncated_para))
                break

        # Re-sort into original logical sequence for coherent reading
        selected.sort(key=lambda x: x[0])
        pruned_text = "\n\n".join(p[1] for p in selected)
        return pruned_text

    def compress(
        self,
        history_text: str,
        observations_text: str,
        query: str,
    ) -> Tuple[str, str, int, int]:
        """Compress history and observations to guarantee prompt fits strict token budget."""
        raw_full = f"{history_text}\n{query}\n{observations_text}"
        raw_tokens = estimate_tokens(raw_full)

        comp_history = self.compress_history(history_text, query)
        comp_obs = self.compress_observations(observations_text, query)

        # Budget safeguard
        system_overhead = 150
        query_tokens = estimate_tokens(query)
        current_total = system_overhead + query_tokens + estimate_tokens(comp_history) + estimate_tokens(comp_obs)

        if current_total > self.max_budget:
            allowed_obs = max(200, self.max_budget - (system_overhead + query_tokens + estimate_tokens(comp_history)))
            allowed_chars = int(allowed_obs * 3.5)
            comp_obs = comp_obs[:allowed_chars] + "... [budget enforced]"

        compressed_full = f"{comp_history}\n{query}\n{comp_obs}"
        comp_tokens = estimate_tokens(compressed_full)
        return comp_history, comp_obs, raw_tokens, comp_tokens
