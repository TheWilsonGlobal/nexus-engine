"""Repair CLI for mixed-dimension / wrong-embedder vector indexes.

    python -m nexus_engine.services.retrieval.repair_embeddings --db <nexus.db> --dry-run
    python -m nexus_engine.services.retrieval.repair_embeddings --db <nexus.db> --apply

``--dry-run`` opens the database read-only and reports, per stored dimension, how many embedding rows exist and
which ones disagree with the configured embedder. ``--apply`` re-embeds exactly the mismatched rows with the
configured embedder (Settings/env: EMBEDDING_PROVIDER, EMBEDDING_MODEL, EMBEDDING_DIM, key), writes them in one
transaction, then pins the index to that embedder. A backup copy (``<db>.pre-repair.bak``) is made first unless
``--no-backup``. With a real provider this calls the embedding API (quota!); it never falls back to mock vectors.

Exit codes: 0 ok, 2 bad arguments / database, 3 embedder unavailable (nothing was written).
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import sqlite3
import sys
from collections import Counter
from contextlib import closing
from pathlib import Path
from typing import Any, Optional, Sequence

from nexus_engine.core.embeddings import EmbedderLike, EmbeddingUnavailable, build_embedder
from nexus_engine.services.retrieval.index_meta import IndexMetaStore

log = logging.getLogger("repair_embeddings")

BATCH = 50


def _open_ro(db: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True, timeout=15.0)


def scan(conn: sqlite3.Connection, expected_dim: int) -> dict[str, Any]:
    """Report dimension distribution and the rows that disagree with ``expected_dim``."""
    dims: Counter[int] = Counter()
    mismatched: list[str] = []
    declared_wrong = 0
    for chunk_id, declared, raw in conn.execute("SELECT chunk_id, dimension, embedding FROM chunk_embeddings"):
        actual = len(json.loads(raw))
        dims[actual] += 1
        if actual != declared:
            declared_wrong += 1
        if actual != expected_dim:
            mismatched.append(chunk_id)
    missing = conn.execute(
        "SELECT COUNT(*) FROM chunks c LEFT JOIN chunk_embeddings e ON c.id = e.chunk_id WHERE e.chunk_id IS NULL"
    ).fetchone()[0]
    total_chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    return {
        "expected_dim": expected_dim,
        "embedding_rows": sum(dims.values()),
        "rows_by_dimension": {str(d): n for d, n in sorted(dims.items())},
        "mismatched_rows": len(mismatched),
        "declared_dimension_wrong": declared_wrong,
        "chunks_total": total_chunks,
        "chunks_without_embedding": missing,
        "mismatched_chunk_ids": mismatched,
    }


def _pin_text(db: Path) -> Optional[str]:
    try:
        with closing(_open_ro(db)) as conn:
            row = conn.execute("SELECT value FROM index_meta WHERE key = 'embedding_pin'").fetchone()
        return row[0] if row else None
    except sqlite3.Error:
        return None  # no index_meta table yet


def apply_repair(db: Path, embedder: EmbedderLike, mismatched_ids: Sequence[str]) -> int:
    """Re-embed ``mismatched_ids`` and write them atomically. Raises EmbeddingUnavailable before any write."""
    ident = embedder.identity
    with closing(sqlite3.connect(str(db), timeout=15.0)) as conn:
        rows = []
        for i in range(0, len(mismatched_ids), BATCH):
            ids = list(mismatched_ids[i : i + BATCH])
            q = ",".join("?" * len(ids))
            rows.extend(conn.execute(f"SELECT id, path, content FROM chunks WHERE id IN ({q})", ids))
        texts = [f"{path}\n{content}".strip() if path else content for _id, path, content in rows]
        new_vecs: list[Any] = []
        for i in range(0, len(texts), BATCH):
            new_vecs.extend(embedder.embed_batch(texts[i : i + BATCH], prefix="passage:"))
        bad = {len(v) for v in new_vecs if len(v) != ident.dim}
        if bad:
            raise EmbeddingUnavailable(f"embedder returned dimensions {sorted(bad)} but is pinned to {ident.dim}")
        with conn:  # single transaction: all rows or none
            for (cid, _p, _c), vec in zip(rows, new_vecs):
                conn.execute(
                    "INSERT OR REPLACE INTO chunk_embeddings (chunk_id, dimension, embedding) VALUES (?, ?, ?)",
                    (cid, ident.dim, json.dumps(vec.tolist())),
                )
        return len(rows)


def main(argv: Optional[Sequence[str]] = None, embedder: Optional[EmbedderLike] = None) -> int:
    ap = argparse.ArgumentParser(prog="repair_embeddings", description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", required=True, help="path to the nexus.db to inspect/repair")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="report only; opens the database read-only")
    mode.add_argument("--apply", action="store_true", help="re-embed mismatched rows with the configured embedder")
    ap.add_argument("--no-backup", action="store_true", help="skip the <db>.pre-repair.bak copy before --apply")
    args = ap.parse_args(argv)

    db = Path(args.db)
    if not db.is_file():
        print(f"error: database not found: {db}", file=sys.stderr)
        return 2
    emb = embedder if embedder is not None else build_embedder(None)
    ident = emb.identity

    try:
        with closing(_open_ro(db)) as conn:
            report = scan(conn, ident.dim)
    except sqlite3.Error as exc:
        print(f"error: cannot read {db}: {exc}", file=sys.stderr)
        return 2

    ids = report.pop("mismatched_chunk_ids")
    report["embedder"] = ident.describe()
    report["pinned_to"] = _pin_text(db)
    report["mode"] = "apply" if args.apply else "dry-run"

    if args.apply:
        if not ids:
            report["repaired_rows"] = 0
            report["note"] = "no mismatched rows; nothing re-embedded"
        else:
            if not args.no_backup:
                backup = db.with_name(db.name + ".pre-repair.bak")
                shutil.copy2(db, backup)
                report["backup"] = str(backup)
            try:
                report["repaired_rows"] = apply_repair(db, emb, ids)
            except EmbeddingUnavailable as exc:
                report["error"] = f"embedder unavailable, nothing written: {exc}"
                print(json.dumps(report, indent=2))
                return 3
        with closing(sqlite3.connect(str(db), timeout=15.0)) as conn:
            remaining = scan(conn, ident.dim)["mismatched_rows"]
        report["mismatched_rows_after"] = remaining
        if remaining == 0 and report["embedding_rows"] > 0:
            IndexMetaStore(str(db)).save_pin(ident)
            report["pinned_to"] = ident.describe()
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(main())
