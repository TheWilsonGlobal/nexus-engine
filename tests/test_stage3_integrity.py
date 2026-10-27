"""Stage 3 data-integrity regressions: refactor safety, LIKE wildcards, sessions route,
chunk limit clamp, cache/registry invalidation, malformed uploads.

Auth uses ``dependency_overrides`` (no reliance on the TestClient user-agent shortcut).
"""
from __future__ import annotations

import hashlib
import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nexus_engine.api.deps import get_current_user
from nexus_engine.core.config import Settings
from nexus_engine.main import create_app
from nexus_engine.schemas.auth import AuthenticatedUser
from nexus_engine.services.storage.database import NexusDatabase
from nexus_engine.services.storage.dedup_operations import analyze_duplicates, apply_refactor
from nexus_engine.services.storage.operations import delete_doc_cascade, list_all_documents

API = "/api/v1"


@pytest.fixture()
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # Open (keyless) mode is only permitted by the startup checks under ENV=development.
    monkeypatch.setenv("ENV", "development")
    settings = Settings(
        audit_db_path=str(tmp_path / "audit.db"),
        database_path=str(tmp_path / "nexus.db"),
        llm_provider="mock",
        embedding_provider="mock",
    )
    application = create_app(settings)
    application.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(
        username="stage3_admin", roles=["admin"], auth_method="test"
    )
    return application


@pytest.fixture()
def client(app) -> TestClient:
    return TestClient(app, client=("127.0.0.1", 50000))


@pytest.fixture()
def container(app):
    return app.state.container


def _counts(db: NexusDatabase) -> dict[str, int]:
    with db._get_conn() as conn:
        return {
            t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("documents", "chunks", "chunk_embeddings", "graph_nodes", "graph_edges")
        }


def _upload(client: TestClient, name: str, data: bytes, **form):
    return client.post(f"{API}/ingestion/upload", files={"file": (name, io.BytesIO(data))}, data=form)


# ---------------------------------------------------------------- REV-01
def test_refactor_on_real_sample_pack_changes_nothing(client, container):
    assert client.post(f"{API}/ingestion/sample-pack").status_code == 200
    db = container.nexus_db
    before = _counts(db)
    assert before["chunks"] > 30 and before["graph_nodes"] > 0  # the SDK-bundled pack is smaller than the sample app's

    # apply_refactor reloads the retriever from the DB, so take the baseline from a DB reload too
    # (a fresh ingest and a DB reload rank slightly differently; that is independent of refactor).
    container.retriever.load_from_db()
    clause_query = "Clause 1 data processor obligations"
    hits_before = [h.chunk_id for h in container.retriever.search(clause_query, 8)]
    graph_before = [cid for cid, _ in container.retriever.graph.rank_chunks(clause_query, 20)]
    assert graph_before, "graph retrieval must work for a clause query before refactor"

    analysis = client.post(f"{API}/ingestion/refactor/analyze").json()
    assert analysis["mergeable_nodes_count"] == 0
    assert analysis["duplicate_chunks_count"] == 0
    assert _counts(db) == before  # analyze is read-only

    res = client.post(f"{API}/ingestion/refactor/apply")
    assert res.status_code == 200
    assert res.json()["merged_nodes"] == 0
    assert _counts(db) == before

    with db._get_conn() as conn:
        clause_nodes = conn.execute("SELECT COUNT(*) FROM graph_nodes WHERE entity_type='clause'").fetchone()[0]
    assert clause_nodes > 10  # SDK-bundled pack has 21; the sample app's has >60

    assert [cid for cid, _ in container.retriever.graph.rank_chunks(clause_query, 20)] == graph_before
    assert [h.chunk_id for h in container.retriever.search(clause_query, 8)] == hits_before


def _fresh_db(tmp_path: Path) -> NexusDatabase:
    return NexusDatabase(str(tmp_path / "unit.db"))


def test_true_duplicate_entity_nodes_still_merge(tmp_path):
    db = _fresh_db(tmp_path)
    db.insert_node("entity:data-controller", "Data Controller", "role")
    db.insert_node("entity:data_controller", "Data  Controller", "role")
    db.insert_node("entity:other", "Other", "role")
    db.insert_edge("entity:data_controller", "entity:other", "mentions")
    # same label, structural (non-entity) nodes: must be left alone
    db.insert_node("d1/a/cl1", "Clause 1", "clause")
    db.insert_node("d2/a/cl1", "Clause 1", "clause")

    with db._get_conn() as conn:
        assert analyze_duplicates(conn)["mergeable_nodes_count"] == 1
        result = apply_refactor(conn)
        assert result["merged_nodes"] == 1
        ids = {r[0] for r in conn.execute("SELECT id FROM graph_nodes")}
        assert ids == {"entity:data-controller", "entity:other", "d1/a/cl1", "d2/a/cl1"}
        edge = conn.execute("SELECT source_id, target_id FROM graph_edges").fetchall()
        assert [tuple(e) for e in edge] == [("entity:data-controller", "entity:other")]


def test_refactor_never_deletes_parent_or_child_chunks(tmp_path):
    db = _fresh_db(tmp_path)
    db.insert_chunk("d:root", "d", "document", "T", "same text", "T")
    db.insert_chunk("d:root2", "d", "document", "T2", "same text", "T2")  # referenced as a parent below
    db.insert_chunk("d:c1", "d", "clause", "C", "child text", "T/C", parent_id="d:root2")
    db.insert_chunk("d:c2", "d", "clause", "C", "child text", "T/C", parent_id="d:root2")
    db.insert_chunk("d:solo", "d", "document", "S", "orphan repeat", "S")
    db.insert_chunk("d:solo2", "d", "document", "S", "orphan repeat", "S")  # true unreferenced duplicate
    db.insert_chunk("d:vec", "d", "clause", "V", "x", "V", embedding=[0.1, 0.2])

    with db._get_conn() as conn:
        analysis = analyze_duplicates(conn)
        assert {c["chunk_id"] for c in analysis["duplicate_chunks"]} == {"d:solo2"}
        apply_refactor(conn)
        left = {r[0] for r in conn.execute("SELECT id FROM chunks")}
    assert left == {"d:root", "d:root2", "d:c1", "d:c2", "d:solo", "d:vec"}


# ---------------------------------------------------------------- REV-13
def _seed_doc(db: NexusDatabase, doc_id: str, content_hash: str = "") -> None:
    db.insert_document(doc_id, f"Title {doc_id}", "text", 1, 0, 0, 0.0, content_hash=content_hash)
    db.insert_chunk(f"{doc_id}:c1", doc_id, "article", "A", f"text {doc_id}", "P")
    db.insert_node(f"{doc_id}:a1", f"Article {doc_id}", "article")
    db.insert_node(f"{doc_id}/b1", f"Sub {doc_id}", "section")
    db.insert_edge(f"{doc_id}:a1", f"{doc_id}/b1", "contains")


@pytest.mark.parametrize("victim,bystander", [("a_b", "a-b"), ("a_b", "axb"), ("%", "zzz"), ("a%", "abc")])
def test_doc_id_wildcards_are_literal_on_delete(tmp_path, victim, bystander):
    db = _fresh_db(tmp_path)
    _seed_doc(db, victim)
    _seed_doc(db, bystander)
    with db._get_conn() as conn:
        by = {r["doc_id"]: r for r in map(dict, list_all_documents(conn))}
        assert by[victim]["node_count"] == 2 and by[victim]["edge_count"] == 1
        assert by[bystander]["node_count"] == 2

        delete_doc_cascade(conn, victim)
        nodes = {r[0] for r in conn.execute("SELECT id FROM graph_nodes")}
        edges = conn.execute("SELECT COUNT(*) FROM graph_edges").fetchone()[0]
    assert nodes == {f"{bystander}:a1", f"{bystander}/b1"}
    assert edges == 1


def test_refactor_duplicate_doc_removal_does_not_touch_lookalike(tmp_path):
    db = _fresh_db(tmp_path)
    with db._get_conn() as conn:
        conn.execute("DELETE FROM documents")
    _seed_doc(db, "a-b", content_hash="h1")
    _seed_doc(db, "a_b", content_hash="h1")  # later duplicate: removed
    with db._get_conn() as conn:
        conn.execute("UPDATE documents SET created_at='2000-01-01' WHERE doc_id='a-b'")
        conn.execute("UPDATE documents SET created_at='2001-01-01' WHERE doc_id='a_b'")
        conn.commit()
        result = apply_refactor(conn)
        assert result["removed_docs"] == 1
        nodes = {r[0] for r in conn.execute("SELECT id FROM graph_nodes")}
        chunks = {r[0] for r in conn.execute("SELECT id FROM chunks")}
    assert nodes == {"a-b:a1", "a-b/b1"}
    assert chunks == {"a-b:c1"}


# ---------------------------------------------------------------- TST-02
def test_sessions_route_with_active_session(client, container):
    container.memory.add_turn("sess-one", "user", "hello there world")
    container.memory.add_turn("sess-one", "assistant", "hi")
    r = client.get(f"{API}/memory/sessions")
    assert r.status_code == 200
    rows = {s["session_id"]: s for s in r.json()["sessions"]}
    assert rows["sess-one"]["active"] is True
    assert rows["sess-one"]["turns"] == 2
    assert rows["sess-one"]["tokens"] == 4


# ---------------------------------------------------------------- TST-08
@pytest.mark.parametrize("limit", [0, -1, 501, 1000000000])
def test_chunks_limit_rejected_out_of_range(client, limit):
    assert client.get(f"{API}/retrieval/chunks", params={"limit": limit}).status_code == 422


def test_chunks_limit_accepts_bounds(client):
    client.post(f"{API}/ingestion/sample-pack")
    assert len(client.get(f"{API}/retrieval/chunks", params={"limit": 1}).json()["chunks"]) == 1
    assert client.get(f"{API}/retrieval/chunks", params={"limit": 500}).status_code == 200
    assert client.get(f"{API}/retrieval/chunks").status_code == 200


# ---------------------------------------------------------------- REV-30 / REV-26
def test_delete_document_invalidates_semantic_cache(client, container):
    assert _upload(client, "cachedoc.md", b"# Cache Doc\n\nSome unique body text.").status_code == 200
    container.memory.promote_verified("what does the cache doc say", "answer from cachedoc", ["cachedoc"])
    assert container.memory.lookup_cache("what does the cache doc say") is not None

    assert client.delete(f"{API}/ingestion/documents/cachedoc").status_code == 200
    assert len(container.memory.cache) == 0
    assert container.memory.lookup_cache("what does the cache doc say") is None


def test_failed_delete_keeps_cache(client, container):
    container.memory.promote_verified("keep me", "answer")
    assert client.delete(f"{API}/ingestion/documents/nonexistent").status_code == 404
    assert len(container.memory.cache) == 1


def test_reset_invalidates_semantic_cache(client, container):
    container.memory.promote_verified("stale question", "stale answer")
    assert client.post(f"{API}/ingestion/reset").status_code == 200
    assert len(container.memory.cache) == 0


def test_reupload_after_delete_is_not_skipped_duplicate(client, container):
    body = b"# Re-upload\n\nBody that is identical both times."
    assert _upload(client, "again.md", body).json()["status"] == "indexed"
    assert client.delete(f"{API}/ingestion/documents/again").status_code == 200
    second = _upload(client, "again.md", body)
    assert second.status_code == 200
    assert second.json()["status"] == "indexed"


def test_refactor_apply_clears_stale_registry_and_cache(client, container):
    body = b"# Registry\n\nBody for registry staleness check."
    first = _upload(client, "registry.md", body).json()
    assert first["status"] == "indexed"
    h = hashlib.sha256(body).hexdigest()
    assert "registry" in container.pipeline._registry

    # An earlier document with the same hash makes `registry` the removable duplicate.
    container.nexus_db.insert_document("registry_orig", "Registry orig", "markdown", 0, 0, 0, 0.0, content_hash=h)
    with container.nexus_db._get_conn() as conn:
        conn.execute("UPDATE documents SET created_at='2000-01-01' WHERE doc_id='registry_orig'")
        conn.commit()
    container.memory.promote_verified("registry question", "registry answer")

    res = client.post(f"{API}/ingestion/refactor/apply")
    assert res.status_code == 200 and res.json()["removed_docs"] == 1

    assert "registry" not in container.pipeline._registry
    assert container.pipeline.get_document("registry") is None
    pointed = container.pipeline.get_document_by_hash(h)
    assert pointed is not None and pointed.doc_id == "registry_orig"
    assert len(container.memory.cache) == 0


def test_rehydrate_registry_drops_entries_missing_from_db(container):
    _ = container.pipeline.ingest_document(b"# Ghost\n\nghost body", "ghost.md")
    assert "ghost" in container.pipeline._registry
    container.nexus_db.delete_document("ghost")
    container.pipeline._rehydrate_registry()
    assert "ghost" not in container.pipeline._registry


# ---------------------------------------------------------------- TST-04 / REV-28
BAD_UPLOADS = [
    ("bad.json", b"{not valid json"),
    ("list.json", b"[1, 2, 3]"),
    ("scalar.json", b'"just a string"'),
    ("shape.json", b'{"document": "oops"}'),
    ("shape2.json", b'{"parts": [1, 2]}'),
    ("binary.json", b"\xff\xfe\x00\x00"),
    ("corrupt.pdf", b"%PDF-1.4\nthis is definitely not a pdf"),
]


@pytest.mark.parametrize("name,data", BAD_UPLOADS)
def test_malformed_upload_is_422_with_generic_message(client, name, data):
    r = _upload(client, name, data)
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert isinstance(detail, str)
    for leak in ("Traceback", "Error:", "Expecting", "line 1", "char ", "AttributeError", "codec", "pypdf", "EOF"):
        assert leak not in detail
    assert client.get(f"{API}/ingestion/documents").json()["total_documents"] == 0


def test_valid_json_upload_still_works(client):
    doc = b'{"document": {"title": "T", "articles": [{"number": 1, "title": "A1", "clauses": ["c one"]}]}}'
    r = _upload(client, "ok.json", doc)
    assert r.status_code == 200 and r.json()["status"] == "indexed"

