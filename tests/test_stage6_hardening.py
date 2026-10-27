"""Stage 6: Host validation, CORS, CSP, cookie flags, audit trail, error sanitising (SEC-07/08/10/12, REV-18/28)."""
from __future__ import annotations

import io
import re

import pytest
from fastapi.testclient import TestClient

from nexus_engine.core.constants import DEFAULT_CORS_ORIGINS
from nexus_engine.main import allowed_hosts, create_app


def _security_rows(app) -> list[dict]:
    return app.state.container.memory.audit.list_records(session_id="security", limit=500)


def _audit_rows(app, action: str) -> list[dict]:
    return [r for r in _security_rows(app) if r["tool"] == "audit_event" and f"action={action} " in r["content"]]


# ---- SEC-07: Host header + CORS -------------------------------------------------------------
@pytest.mark.parametrize("host", ["evil.example", "evil.example:8766", "127.0.0.1.evil.example", "localhost.evil.example", ""])
def test_unknown_host_is_rejected_with_400(app, host):
    c = TestClient(app, base_url="http://testserver")
    r = c.get("/health", headers={"Host": host})
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_host"


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.1:8766", "localhost", "localhost:5200", "[::1]", "[::1]:8766", "testserver", "LOCALHOST"])
def test_loopback_hosts_are_accepted(app, host):
    r = TestClient(app).get("/health", headers={"Host": host})
    assert r.status_code == 200, (host, r.text)


def test_allowed_hosts_env_extends_the_list(settings, monkeypatch):
    monkeypatch.setenv("ALLOWED_HOSTS", "backend, api.example.org")
    assert {"backend", "api.example.org"} <= set(allowed_hosts(settings))
    c = TestClient(create_app(settings))
    assert c.get("/health", headers={"Host": "backend:8766"}).status_code == 200
    assert c.get("/health", headers={"Host": "other"}).status_code == 400


def test_wildcard_in_allowed_hosts_is_ignored(settings, monkeypatch):
    monkeypatch.setenv("ALLOWED_HOSTS", "*")
    assert "*" not in allowed_hosts(settings)


def test_testserver_host_only_in_development(settings, monkeypatch):
    monkeypatch.setenv("ENV", "production")
    assert "testserver" not in allowed_hosts(settings)


def test_no_placeholder_cors_origin_and_restricted_preflight(app):
    assert not any("dummy" in o for o in DEFAULT_CORS_ORIGINS)
    c = TestClient(app)
    ok = c.options(
        "/api/v1/agent/run",
        headers={
            "Origin": "http://127.0.0.1:5200",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert ok.status_code == 200
    assert ok.headers["access-control-allow-origin"] == "http://127.0.0.1:5200"
    assert "*" not in ok.headers["access-control-allow-headers"]
    assert "PUT" not in ok.headers["access-control-allow-methods"]
    bad_method = c.options(
        "/api/v1/agent/run",
        headers={"Origin": "http://127.0.0.1:5200", "Access-Control-Request-Method": "PUT"},
    )
    assert bad_method.status_code == 400
    bad_header = c.options(
        "/api/v1/agent/run",
        headers={
            "Origin": "http://127.0.0.1:5200",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "x-evil",
        },
    )
    assert bad_header.status_code == 400
    foreign = c.options(
        "/api/v1/agent/run",
        headers={"Origin": "http://dummy-cors-origin:5200", "Access-Control-Request-Method": "POST"},
    )
    assert "access-control-allow-origin" not in foreign.headers


# ---- REV-18: CSP ---------------------------------------------------------------------------------
@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_docs_paths_are_exempt_from_the_strict_csp(app, path):
    r = TestClient(app).get(path)
    assert r.status_code == 200
    assert "content-security-policy" not in r.headers
    assert r.headers["x-content-type-options"] == "nosniff"


def test_api_responses_keep_the_strict_csp(app):
    r = TestClient(app).get("/health")
    assert "script-src 'self'" in r.headers["content-security-policy"]


# ---- SEC-08: cookie ----------------------------------------------------------------------------------
def _login(app, test_passwords, monkeypatch=None):
    c = TestClient(app, client=("127.0.0.1", 50000))
    return c.post("/api/v1/auth/login", json={"username": "admin", "password": test_passwords["admin"]})


def test_session_cookie_flags_in_development(app, test_passwords):
    cookie = _login(app, test_passwords).headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert "secure" not in cookie.replace("samesite", "")  # plain-http dev server: Secure would drop the cookie


def test_session_cookie_is_secure_outside_development(app, test_passwords, monkeypatch):
    monkeypatch.setenv("ENV", "production")
    cookie = _login(app, test_passwords).headers["set-cookie"].lower()
    assert re.search(r";\s*secure", cookie) and "httponly" in cookie and "samesite=strict" in cookie


# ---- SEC-10: audit trail ---------------------------------------------------------------------------
def test_destructive_actions_are_audited_with_user_and_ip(app, client):
    assert client.post("/api/v1/ingestion/sample-pack").status_code == 200
    up = client.post(
        "/api/v1/ingestion/upload",
        files={"file": ("audited.txt", io.BytesIO(b"# Audited\n\nSome audited content here."), "text/plain")},
    )
    assert up.status_code == 200
    doc_id = up.json()["doc_id"]
    assert client.post("/api/v1/ingestion/refactor/apply").status_code == 200
    assert client.delete(f"/api/v1/ingestion/documents/{doc_id}").status_code == 200
    sid = client.post("/api/v1/memory/sessions", json={"session_id": "aud-sess"}).json()["session_id"]
    assert client.delete(f"/api/v1/memory/sessions/{sid}").status_code == 200
    assert client.post("/api/v1/telemetry/tokens/reset").status_code == 200
    assert client.post("/api/v1/ingestion/reset").status_code == 200

    expect = {
        "ingestion.upload": "doc_id=" + doc_id,
        "ingestion.refactor_apply": "",
        "ingestion.delete_document": "doc_id=" + doc_id,
        "memory.delete_session": "session_id=aud-sess",
        "telemetry.tokens_reset": "",
        "ingestion.reset": "",
    }
    for action, target in expect.items():
        rows = _audit_rows(app, action)
        assert rows, f"no audit row for {action}"
        content = rows[0]["content"]
        assert "user=fixture_admin" in content and "ip=127.0.0.1" in content and "outcome=ok" in content, content
        assert target in content, content


def test_audit_fields_are_single_line_and_capped():
    from nexus_engine.api.audit import MAX_FIELD, clean_field

    forged = clean_field("doc\naction=forged\r\x00user=root")
    assert "\n" not in forged and "\r" not in forged and "\x00" not in forged
    assert len(clean_field("a" * 10_000)) == MAX_FIELD


def test_failed_login_logs_a_hash_not_the_username(app, anon_client):
    secret_like = "Hunter2-my-real-password"
    r = anon_client.post("/api/v1/auth/login", json={"username": secret_like, "password": "x"})
    assert r.status_code == 401
    rows = [x for x in _security_rows(app) if x["tool"] == "auth_login" and "login_failed" in x["content"]]
    assert rows
    for row in rows:
        assert secret_like not in row["content"] and "Hunter2" not in row["content"]
        assert re.search(r"user_hash=[0-9a-f]{12} ip=127\.0\.0\.1", row["content"])
    # same input -> same tag (correlatable), different input -> different tag
    anon_client.post("/api/v1/auth/login", json={"username": secret_like, "password": "y"})
    anon_client.post("/api/v1/auth/login", json={"username": "someone-else", "password": "y"})
    tags = [re.search(r"user_hash=([0-9a-f]{12})", x["content"]).group(1)
            for x in _security_rows(app) if "login_failed" in x["content"]]
    assert len(set(tags)) == 2


# ---- SEC-12 / REV-28: sanitised errors --------------------------------------------------------------
SECRET_PATH = r"C:\Users\victim\secret.db"


def test_handler_500s_carry_a_correlation_id_not_the_exception(settings, make_client, caplog):
    app = create_app(settings)
    client = make_client(app)

    def boom(*_a, **_k):
        raise RuntimeError(f"sqlite3.OperationalError: unable to open {SECRET_PATH}")

    c = app.state.container
    c.pipeline.ingest_sample_pack = boom
    c.pipeline.reset_knowledge_base = boom
    c.nexus_db.analyze_knowledge_base = boom
    c.nexus_db.apply_knowledge_refactor = boom
    for method, path in [
        ("post", "/api/v1/ingestion/sample-pack"),
        ("post", "/api/v1/ingestion/reset"),
        ("post", "/api/v1/ingestion/refactor/analyze"),
        ("post", "/api/v1/ingestion/refactor/apply"),
    ]:
        with caplog.at_level("ERROR"):
            r = getattr(client, method)(path)
        assert r.status_code == 500, path
        detail = r.json()["detail"]
        assert SECRET_PATH not in r.text and "OperationalError" not in r.text, path
        cid = re.search(r"Reference: ([0-9a-f]{12})", detail)
        assert cid, detail
        assert any(cid.group(1) in rec.getMessage() and SECRET_PATH in rec.getMessage() for rec in caplog.records)

    failed = _audit_rows(app, "ingestion.reset")
    assert failed and "outcome=failed" in failed[0]["content"]


def test_upload_pipeline_failure_is_masked(settings, make_client):
    app = create_app(settings)
    client = make_client(app)
    app.state.container.pipeline.ingest_document = lambda **_k: (_ for _ in ()).throw(RuntimeError(SECRET_PATH))
    r = client.post("/api/v1/ingestion/upload", files={"file": ("a.txt", io.BytesIO(b"hello world"), "text/plain")})
    assert r.status_code == 500 and SECRET_PATH not in r.text and "Reference:" in r.json()["detail"]


def test_malformed_json_upload_is_422_not_500(client):
    for body in (b"{not json", b"[1, 2, 3]", b'"just a string"'):
        r = client.post("/api/v1/ingestion/upload", files={"file": ("bad.json", io.BytesIO(body), "application/json")})
        assert r.status_code == 422, (body, r.status_code, r.text)
        assert "Traceback" not in r.text


def test_corrupt_pdf_is_422(client):
    r = client.post("/api/v1/ingestion/upload", files={"file": ("bad.pdf", io.BytesIO(b"%PDF-1.4 nope"), "application/pdf")})
    assert r.status_code == 422
