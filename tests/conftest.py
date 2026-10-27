"""SDK Contract Test Fixtures for nexus-engine."""
from __future__ import annotations

import os
import pytest
from nexus_engine.core.config import Settings
from nexus_engine.core.container import Container, build_container


@pytest.fixture()
def settings(tmp_path) -> Settings:
    return Settings(
        audit_db_path=str(tmp_path / "audit.db"),
        database_path=str(tmp_path / "nexus.db"),
        llm_provider="mock",
        embedding_provider="mock",
        network_enabled=True,
        sandbox_enabled=True,
        sandbox_timeout_s=3.0,
        sandbox_memory_mb=128,
    )


@pytest.fixture()
def container(settings: Settings) -> Container:
    return build_container(settings)
