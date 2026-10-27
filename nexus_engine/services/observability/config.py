"""Observability configuration for Langfuse and telemetry tracing."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ObservabilityConfig:
    public_key: Optional[str] = None
    secret_key: Optional[str] = None
    base_url: str = "https://cloud.langfuse.com"
    enabled: bool = False

    @classmethod
    def from_env(cls) -> "ObservabilityConfig":
        pub = os.environ.get("LANGFUSE_PUBLIC_KEY") or os.environ.get("COGNI_LANGFUSE_PUBLIC_KEY")
        sec = os.environ.get("LANGFUSE_SECRET_KEY") or os.environ.get("COGNI_LANGFUSE_SECRET_KEY")
        base = (
            os.environ.get("LANGFUSE_BASE_URL")
            or os.environ.get("LANGFUSE_HOST")
            or os.environ.get("COGNI_LANGFUSE_BASE_URL")
            or "https://cloud.langfuse.com"
        )
        # Strip quotes if user provided them in .env
        if pub:
            pub = pub.strip("\"' ")
        if sec:
            sec = sec.strip("\"' ")
        if base:
            base = base.strip("\"' ")

        enabled = bool(pub and sec and not pub.startswith("your_"))
        return cls(public_key=pub, secret_key=sec, base_url=base, enabled=enabled)
