"""Embedding subsystem.

An ``Embedder`` is built from configuration (``Settings`` or, as a compatibility path, the same env vars)
and pinned to ONE provider/model/dimension. There is deliberately no cross-provider fallback and no silent
fall-through to deterministic mock vectors for a real provider: vectors from different providers or
dimensions live in different spaces and must never share an index. A failed call raises
``EmbeddingUnavailable``; callers decide how to degrade (the vector store keeps sparse/graph retrieval).
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass
from typing import Any, Optional, Protocol

import numpy as np

from nexus_engine.core.constants import DEFAULT_EMBEDDING_URL

log = logging.getLogger(__name__)

MOCK_DIM = 4096
REMOTE_DIM = 768
LOCAL_SERVER_DEFAULT_URL = "http://127.0.0.1:5819/v1/embeddings"
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
BREAKER_COOLDOWN_S = 10.0
_TOKEN = re.compile(r"[a-z0-9]+")
STOPWORDS = frozenset(
    "a an the of to in on for and or is are was were be by with as at it its this that "
    "what which who how do does did i me my you your we our can could should would "
    "please about from into".split()
)


def _stem(tok: str) -> str:
    if len(tok) > 4 and tok.endswith("ies"):
        return tok[:-3] + "y"
    if len(tok) > 3 and tok.endswith("s") and not tok.endswith("ss"):
        return tok[:-1]
    return tok


def tokenize(text: str) -> list[str]:
    return [_stem(t) for t in _TOKEN.findall(text.lower()) if t not in STOPWORDS]


def _bucket(token: str, dim: int) -> int:
    return zlib.crc32(token.encode("utf-8")) % dim


def embed_mock(text: str, dim: int = MOCK_DIM) -> np.ndarray:
    """Deterministic local bag-of-words embedding of size ``dim`` (default MOCK_DIM)."""
    vec = np.zeros(dim, dtype=np.float32)
    toks = tokenize(text)
    for t in toks:
        vec[_bucket(t, dim)] += 1.0
    for a, b in zip(toks, toks[1:]):
        vec[_bucket(a + "_" + b, dim)] += 0.5
    norm = float(np.linalg.norm(vec))
    return vec / norm if norm > 0 else vec


class EmbeddingUnavailable(RuntimeError):
    """The configured embedder could not produce vectors (offline, 429, bad response, wrong dimension)."""


@dataclass(frozen=True)
class EmbedderIdentity:
    """What an index is pinned to: vectors from a different identity must not be mixed into it."""

    provider: str
    model: str
    dim: int

    def describe(self) -> str:
        return f"{self.provider}:{self.model}@{self.dim}"


class EmbedderLike(Protocol):
    """The surface the vector store and semantic cache depend on (inject fakes in tests)."""

    @property
    def identity(self) -> EmbedderIdentity: ...

    def embed(self, text: str, prefix: str = "") -> np.ndarray: ...

    def embed_batch(self, texts: list[str], prefix: str = "") -> list[np.ndarray]: ...


class _Breaker:
    """Per-provider circuit breaker: after a failure the provider is skipped for a short cooldown."""

    def __init__(self) -> None:
        self._until = 0.0
        self._lock = threading.Lock()

    def is_open(self) -> bool:
        with self._lock:
            return time.monotonic() < self._until

    def trip(self, cooldown_s: float = BREAKER_COOLDOWN_S) -> None:
        with self._lock:
            self._until = time.monotonic() + cooldown_s

    def reset(self) -> None:
        with self._lock:
            self._until = 0.0


_BREAKERS: dict[str, _Breaker] = {"gemini": _Breaker(), "local_server": _Breaker()}


def breaker_for(provider: str) -> _Breaker:
    """Separate breaker per provider (REV-33): a Gemini outage must not disable the local embedder."""
    key = "gemini" if provider == "gemini" else "local_server"
    return _BREAKERS[key]


@dataclass(frozen=True)
class EmbeddingConfig:
    provider: str = "mock"  # "gemini" | "remote" | "local_server" | "mock"
    model: str = "text-embedding-004"
    dim: int = REMOTE_DIM
    url: str = LOCAL_SERVER_DEFAULT_URL
    local_model: str = "multilingual-e5-base"
    api_key: Optional[str] = None
    gemini_timeout_s: float = 8.0
    local_timeout_s: float = 0.8

    @classmethod
    def from_settings(cls, settings: Any) -> "EmbeddingConfig":
        provider = str(getattr(settings, "embedding_provider", "mock") or "mock")
        url = str(getattr(settings, "embedding_url", "") or "")
        if not url or url == DEFAULT_EMBEDDING_URL:  # the shipped constant is a placeholder, not an endpoint
            url = LOCAL_SERVER_DEFAULT_URL
        model = str(getattr(settings, "embedding_model", "") or "text-embedding-004")
        # For the local/remote providers the model name the server expects is configured separately.
        local_model = os.environ.get("EMBEDDING_LOCAL_MODEL") or "multilingual-e5-base"
        return cls(
            provider=provider,
            model=model,
            dim=int(getattr(settings, "embedding_dim", REMOTE_DIM) or REMOTE_DIM),
            url=url,
            local_model=local_model,
            api_key=getattr(settings, "gemini_embedding_api_key", None) or None,
        )

    @classmethod
    def from_env(cls) -> "EmbeddingConfig":
        """Compatibility path for call sites that have no Settings object; mirrors Settings.from_env."""
        env = os.environ
        key = (
            env.get("GEMINI_EMBEDDING_API_KEY")
            or env.get("GEMINI_API_KEY")
            or env.get("GEMINI_LLM__API_KEY")
            or env.get("GEMINI_LLM_API_KEY")
            or None
        )
        return cls(
            provider=env.get("EMBEDDING_PROVIDER") or env.get("COGNI_EMBEDDING_PROVIDER") or "gemini",
            model=env.get("EMBEDDING_MODEL") or env.get("COGNI_EMBEDDING_MODEL") or "text-embedding-004",
            dim=int(env.get("EMBEDDING_DIM") or REMOTE_DIM),
            url=env.get("EMBEDDING_URL") or env.get("COGNI_EMBEDDING_URL") or LOCAL_SERVER_DEFAULT_URL,
            local_model=env.get("EMBEDDING_LOCAL_MODEL")
            or env.get("COGNI_EMBEDDING_LOCAL_MODEL")
            or "multilingual-e5-base",
            api_key=key,
        )


def _normalize_gemini_model(model: str) -> str:
    m = model.strip()
    if m.startswith("models/"):
        m = m[len("models/") :]
    if m in ("text-embedding-004", "embedding-001"):
        return "gemini-embedding-001"
    return m


def _unit(values: list[float]) -> np.ndarray:
    raw = np.array(values, dtype=np.float32)
    norm = float(np.linalg.norm(raw))
    return raw / norm if norm > 0 else raw


class Embedder:
    """Config-driven embedder pinned to one provider/model/dimension."""

    def __init__(self, config: EmbeddingConfig, mock_dim: int = MOCK_DIM) -> None:
        self.config = config
        if config.provider == "mock":
            model, dim = "deterministic-bow", int(mock_dim)
        elif config.provider == "gemini":
            model, dim = _normalize_gemini_model(config.model), int(config.dim)
        else:
            model, dim = config.local_model, int(config.dim)
        self._identity = EmbedderIdentity(provider=config.provider, model=model, dim=dim)

    @property
    def identity(self) -> EmbedderIdentity:
        return self._identity

    # ---- public API --------------------------------------------------
    def embed(self, text: str, prefix: str = "") -> np.ndarray:
        return self.embed_batch([text], prefix=prefix)[0]

    def embed_batch(self, texts: list[str], prefix: str = "") -> list[np.ndarray]:
        if not texts:
            return []
        provider = self.config.provider
        if provider == "mock":
            return [embed_mock(t, self._identity.dim) for t in texts]
        if provider == "gemini":
            vecs = self._gemini(texts, "RETRIEVAL_QUERY" if "query" in prefix.lower() else "RETRIEVAL_DOCUMENT")
        elif provider in ("remote", "local_server"):
            vecs = self._local([f"{prefix} {t}".strip() if prefix else t for t in texts])
        else:
            raise EmbeddingUnavailable(f"unknown embedding provider {provider!r}")
        for v in vecs:
            if v.shape[0] != self._identity.dim:
                raise EmbeddingUnavailable(
                    f"{self._identity.describe()} returned a {v.shape[0]}-d vector; refusing to mix dimensions"
                )
        return vecs

    # ---- providers ---------------------------------------------------
    def _gemini(self, inputs: list[str], task_type: str) -> list[np.ndarray]:
        cfg = self.config
        breaker = breaker_for("gemini")
        if not cfg.api_key:
            raise EmbeddingUnavailable("gemini embedding api key is not configured")
        if breaker.is_open():
            raise EmbeddingUnavailable("gemini embedding circuit breaker open")
        model = self._identity.model
        url = f"{GEMINI_BASE_URL}/models/{model}:batchEmbedContents"
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "NexusEngine/0.3.0",
            "x-goog-api-key": cfg.api_key,  # header, never the URL (keys leak into logs/proxies)
        }
        results: list[np.ndarray] = []
        batch_size = 50
        for i in range(0, len(inputs), batch_size):
            requests = [
                {
                    "model": f"models/{model}",
                    "content": {"parts": [{"text": text}]},
                    "taskType": task_type,
                    "outputDimensionality": self._identity.dim,
                }
                for text in inputs[i : i + batch_size]
            ]
            body = json.dumps({"requests": requests}).encode("utf-8")
            results.extend(self._gemini_post(url, headers, body, breaker))
        if len(results) != len(inputs):
            raise EmbeddingUnavailable(f"gemini returned {len(results)} embeddings for {len(inputs)} inputs")
        return results

    def _gemini_post(self, url: str, headers: dict[str, str], body: bytes, breaker: _Breaker) -> list[np.ndarray]:
        last: Optional[Exception] = None
        for attempt in range(3):
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.config.gemini_timeout_s) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                return [_unit(item.get("values", [])) for item in data.get("embeddings", [])]
            except urllib.error.HTTPError as exc:
                last = exc
                if exc.code == 429 and attempt < 2:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                break
            except Exception as exc:
                last = exc
                break
        breaker.trip()
        log.warning("Gemini embeddings call failed (%s); dense leg unavailable, NOT falling back to mock", last)
        raise EmbeddingUnavailable(f"gemini embeddings failed: {last}")

    def _local(self, payloads: list[str]) -> list[np.ndarray]:
        breaker = breaker_for(self.config.provider)
        if breaker.is_open():
            raise EmbeddingUnavailable("local embedding circuit breaker open")
        try:
            body = json.dumps({"model": self.config.local_model, "input": payloads}).encode("utf-8")
            req = urllib.request.Request(
                self.config.url,
                data=body,
                headers={"Content-Type": "application/json", "User-Agent": "NexusEngine/0.3.0"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=self.config.local_timeout_s) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            items = sorted(data.get("data", []), key=lambda x: x.get("index", 0))
            vecs = [_unit(item["embedding"]) for item in items]
        except Exception as exc:
            breaker.trip()
            log.warning("Local embeddings call to %s failed (%s); NOT falling back to mock", self.config.url, exc)
            raise EmbeddingUnavailable(f"local embeddings failed: {exc}") from exc
        if len(vecs) != len(payloads):
            raise EmbeddingUnavailable(f"local embedder returned {len(vecs)} embeddings for {len(payloads)} inputs")
        return vecs


def build_embedder(settings: Any = None) -> Embedder:
    """Embedder from Settings (preferred) or, with no settings, from the environment."""
    cfg = EmbeddingConfig.from_settings(settings) if settings is not None else EmbeddingConfig.from_env()
    return Embedder(cfg)


def default_embedder() -> Embedder:
    return build_embedder(None)


def embed(text: str, prefix: str = "") -> np.ndarray:
    """Compatibility wrapper using the env-configured embedder. Raises EmbeddingUnavailable on failure."""
    return default_embedder().embed(text, prefix)


def embed_batch(texts: list[str], prefix: str = "") -> list[np.ndarray]:
    return default_embedder().embed_batch(texts, prefix)


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity between two normalized vectors of the SAME dimension.

    Mismatched dimensions score 0.0; callers that care (semantic cache, vector store) check the dimension
    explicitly and log instead of relying on this.
    """
    if len(a) != len(b):
        return 0.0
    return float(np.dot(a, b))
