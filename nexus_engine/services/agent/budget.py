"""Token budget manager and cost analytics for enterprise ReAct execution."""
from __future__ import annotations

import math
import threading
from typing import Optional
from pydantic import BaseModel, Field

# Model Pricing per 1M tokens (USD)
PRICING_PER_1M = {
    "gemini-3.5-flash-lite": {"prompt": 0.075, "completion": 0.30},
    "text-embedding-004": {"prompt": 0.025, "completion": 0.0},
    "default": {"prompt": 0.15, "completion": 0.60},
}


def estimate_tokens(text: str) -> int:
    """Fast, reliable token estimation based on character and subword heuristics."""
    if not text:
        return 0
    # Rule of thumb for English/multilingual tokenizers: ~3.8 characters per token
    return max(1, math.ceil(len(text) / 3.8))


def calculate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Calculate USD cost for prompt and completion token counts."""
    pricing = PRICING_PER_1M.get(model, PRICING_PER_1M["default"])
    cost = (prompt_tokens * pricing["prompt"] + completion_tokens * pricing["completion"]) / 1_000_000.0
    return round(cost, 7)


def calculate_saved_cost(model: str, saved_tokens: int) -> float:
    """Calculate USD savings when LLM invocation is avoided (e.g. Cache Hit or Compression)."""
    pricing = PRICING_PER_1M.get(model, PRICING_PER_1M["default"])
    # Avoided prompt tokens count at prompt price rate
    return round((saved_tokens * pricing["prompt"]) / 1_000_000.0, 7)


class TokenBudgetReport(BaseModel):
    raw_tokens: int = Field(..., description="Uncompressed tokens before pruning")
    compressed_tokens: int = Field(..., description="Tokens after context compression")
    compression_ratio: float = Field(..., description="Percentage of tokens pruned (0.0 - 1.0)")
    budget_limit: int = Field(8192, description="Target context token ceiling")
    budget_exceeded: bool = Field(False, description="Whether prompt exceeded limit")
    tokens_saved_by_cache: int = Field(0, description="Tokens avoided via cache hit")
    estimated_cost_usd: float = Field(0.0, description="Estimated inference cost in USD")
    cost_saved_usd: float = Field(0.0, description="Estimated cost avoided in USD")


class CumulativeTokenMetrics(BaseModel):
    total_requests: int = 0
    total_cache_hits: int = 0
    total_raw_tokens: int = 0
    total_compressed_tokens: int = 0
    total_tokens_saved_cache: int = 0
    total_cost_usd: float = 0.0
    total_cost_saved_usd: float = 0.0
    avg_compression_ratio: float = 0.0


class TokenBudgetManager:
    """Thread-safe manager tracking token budgets, compression savings, and spend."""

    def __init__(self, budget_limit: int = 8192, default_model: str = "gemini-3.5-flash-lite") -> None:
        self.budget_limit = budget_limit
        self.default_model = default_model
        self._lock = threading.Lock()
        self._metrics = CumulativeTokenMetrics()

    def build_report(
        self,
        raw_tokens: int,
        compressed_tokens: int,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cache_hit: bool = False,
        cache_saved_tokens: int = 0,
        model: Optional[str] = None,
    ) -> TokenBudgetReport:
        chosen_model = model or self.default_model
        if raw_tokens > 0:
            ratio = max(0.0, min(1.0, round(1.0 - (compressed_tokens / float(raw_tokens)), 4)))
        else:
            ratio = 0.0

        budget_exceeded = compressed_tokens > self.budget_limit
        cost_usd = calculate_cost(chosen_model, prompt_tokens, completion_tokens)
        cost_saved = calculate_saved_cost(chosen_model, cache_saved_tokens if cache_hit else max(0, raw_tokens - compressed_tokens))

        report = TokenBudgetReport(
            raw_tokens=raw_tokens,
            compressed_tokens=compressed_tokens,
            compression_ratio=ratio,
            budget_limit=self.budget_limit,
            budget_exceeded=budget_exceeded,
            tokens_saved_by_cache=cache_saved_tokens if cache_hit else 0,
            estimated_cost_usd=cost_usd,
            cost_saved_usd=cost_saved,
        )

        with self._lock:
            self._metrics.total_requests += 1
            if cache_hit:
                self._metrics.total_cache_hits += 1
                self._metrics.total_tokens_saved_cache += cache_saved_tokens
            self._metrics.total_raw_tokens += raw_tokens
            self._metrics.total_compressed_tokens += compressed_tokens
            self._metrics.total_cost_usd = round(self._metrics.total_cost_usd + cost_usd, 7)
            self._metrics.total_cost_saved_usd = round(self._metrics.total_cost_saved_usd + cost_saved, 7)
            if self._metrics.total_raw_tokens > 0:
                self._metrics.avg_compression_ratio = round(
                    1.0 - (self._metrics.total_compressed_tokens / float(self._metrics.total_raw_tokens)), 4
                )

        return report

    def get_metrics(self) -> CumulativeTokenMetrics:
        with self._lock:
            return self._metrics.model_copy()

    def reset_metrics(self) -> None:
        with self._lock:
            self._metrics = CumulativeTokenMetrics()
