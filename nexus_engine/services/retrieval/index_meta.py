"""Persistent per-index metadata: the embedder identity (provider, model, dim) an index is pinned to.

Stored in the index database itself (table ``index_meta``) so the pin survives restarts and travels with
the data. Uses its own short-lived connections; it never touches the other tables.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from typing import Optional

from nexus_engine.core.embeddings import EmbedderIdentity

_PIN_KEY = "embedding_pin"
_DDL = "CREATE TABLE IF NOT EXISTS index_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"


class IndexMetaStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=15.0)
        conn.execute(_DDL)
        return conn

    def load_pin(self) -> Optional[EmbedderIdentity]:
        with closing(self._conn()) as conn:
            row = conn.execute("SELECT value FROM index_meta WHERE key = ?", (_PIN_KEY,)).fetchone()
        if not row:
            return None
        data = json.loads(row[0])
        return EmbedderIdentity(provider=str(data["provider"]), model=str(data["model"]), dim=int(data["dim"]))

    def save_pin(self, pin: EmbedderIdentity) -> None:
        payload = json.dumps({"provider": pin.provider, "model": pin.model, "dim": pin.dim})
        with closing(self._conn()) as conn:
            conn.execute("INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)", (_PIN_KEY, payload))
            conn.commit()

    def clear_pin(self) -> None:
        with closing(self._conn()) as conn:
            conn.execute("DELETE FROM index_meta WHERE key = ?", (_PIN_KEY,))
            conn.commit()
