"""Small sample corpus (legal / enterprise) loaded from data/corpus/ with embedded fallback."""
from __future__ import annotations

from nexus_engine.core.data_loader import load_corpus

SAMPLE_DOCUMENTS: dict[str, str] = load_corpus()
