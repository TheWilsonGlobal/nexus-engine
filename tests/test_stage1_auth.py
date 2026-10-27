"""Stage 1: authentication, identity, startup safety, session ownership and slim /health."""
from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nexus_engine.api import deps
from nexus_engine.api.v1 import health as health_module
from nexus_engine.core.config import Settings
from nexus_engine.core.startup_checks import StartupCheckError, is_loopback_host, run_startup_checks
from nexus_engine.main import create_app
from nexus_engine.services.security import auth_manager as am
from nexus_engine.services.security import bootstrap
from nexus_engine.services.security.auth_manager import (
    AuthConfigError,
    AuthManager,
    hash_password,
    load_users_from_env,
    parse_hash,
    verify_password,
)

REPO = Path(__file__).resolve().parents[1]
SPOOF_AGENTS = ["testclient", "TestClient/evil", "testclient-x", "Mozilla/5.0"]
PROTECTED = [
    ("GET", "/api/v1/memory/stats"),
    ("GET", "/api/v1/memory/sessions"),
    ("GET", "/api/v1/memory/audit"),
    ("GET", "/api/v1/telemetry/traces"),
    ("GET", "/api/v1/health/details"),
    ("POST", "/api/v1/agent/run"),
]


# ---- helpers -------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def strong_hash() -> str:
    return hash_password("a-very-long-test-password")  # production cost, computed once


def _login(client: TestClient, username: str, password: str) -> dict[str, str]:
    r = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    client.cookies.clear()
    return {"Authorization": f"Bearer {r.json()['token']}"}


@pytest.fixture()
def second_operator(app):
    pw = "op2-test-pw"
    app.state.container.auth_mgr.add_user("operator2", hash_password(pw, iterations=1000), ["operator", "analyst"])
    return pw


# ---- 1. User-Agent spoofing ------------------------------------------------------------------
def test_testclient_is_not_loopback():
    class Peer:
        host = "testclient"

    class Req:
        client = Peer()

    assert deps.is_loopback(Req()) is False  # type: ignore[arg-type]


@pytest.mark.parametrize("ua", SPOOF_AGENTS)
@pytest.mark.parametrize("mode", ["open", "api_key"])
def test_user_agent_spoof_gets_401_in_every_mode(settings, ua, mode):
    cfg = settings if mode == "open" else replace(settings, api_key="k-for-spoof-test")
    c = TestClient(create_app(cfg), headers={"User-Agent": ua})  # default peer host is "testclient"
    for method, path in PROTECTED:
        r = c.request(method, path, json={"query": "hi"} if method == "POST" else None)
        assert r.status_code == 401, (mode, ua, method, path, r.status_code)


# ---- 2. open mode only in development on loopback -----------------------------------------------
def test_open_mode_refused_outside_development(settings):
    with pytest.raises(StartupCheckError, match="open mode"):
        run_startup_checks(settings, env={})
    with pytest.raises(StartupCheckError, match="open mode"):
        run_startup_checks(settings, env={"ENV": "production"})


def test_open_mode_refused_on_non_loopback_bind(settings):
    with pytest.raises(StartupCheckError, match="loopback"):
        run_startup_checks(replace(settings, backend_host="0.0.0.0"), env={"ENV": "development"})
    for host in ("127.0.0.1", "::1", "localhost", "[::1]"):
        assert is_loopback_host(host)
        run_startup_checks(replace(settings, backend_host=host), env={"ENV": "development"})
    assert not is_loopback_host("10.0.0.5") and not is_loopback_host("")


def test_create_app_raises_when_unsafe(settings, monkeypatch):
    for name in ("ENV", "NEXUS_ENV", "COGNI_ENV"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(StartupCheckError):
        create_app(settings)
    monkeypatch.setenv("ENV", "development")
    create_app(settings)  # loopback + development: fine
    with pytest.raises(StartupCheckError):
        create_app(replace(settings, backend_host="0.0.0.0"))


def test_non_dev_requires_admin_hash(settings, strong_hash):
    keyed = replace(settings, api_key="k")
    with pytest.raises(StartupCheckError, match="ADMIN_PASSWORD_HASH"):
        run_startup_checks(keyed, env={"ENV": "production"})
    run_startup_checks(keyed, env={"ENV": "production", "ADMIN_PASSWORD_HASH": strong_hash})


def test_startup_rejects_retired_default_and_weak_hashes(settings):
    keyed = replace(settings, api_key="k")
    retired = hash_password("admin123")  # the old built-in password, at full cost
    with pytest.raises(StartupCheckError, match="retired default"):
        run_startup_checks(keyed, env={"ENV": "production", "ADMIN_PASSWORD_HASH": retired})
    weak = hash_password("some-other-password", iterations=1000)
    with pytest.raises(StartupCheckError, match="iterations"):
        run_startup_checks(keyed, env={"ENV": "development", "ADMIN_PASSWORD_HASH": weak})
    with pytest.raises(StartupCheckError, match="not a valid"):
        run_startup_checks(keyed, env={"ENV": "development", "ADMIN_PASSWORD_HASH": "plaintext"})


# ---- 3. credentials -------------------------------------------------------------------------------
def test_no_default_credentials_or_static_salts_in_source():
    banned = re.compile(r"admin123|operator123|audit123|cogni_salt", re.I)
    offenders = [
        p.relative_to(REPO).as_posix()
        for p in (REPO / "nexus_engine").rglob("*.py")
        if banned.search(p.read_text(encoding="utf-8"))
    ]
    # startup_checks keeps the retired passwords ONLY to refuse hashes of them
    assert offenders == ["nexus_engine/core/startup_checks.py"]


def test_seeded_credentials_fail_login(settings):
    app = create_app(settings)  # no users provisioned, no *_PASSWORD_HASH in env
    assert not app.state.container.auth_mgr.has_users()
    c = TestClient(app)
    for user, pw in (("admin", "admin123"), ("operator", "operator123"), ("auditor", "audit123")):
        assert c.post("/api/v1/auth/login", json={"username": user, "password": pw}).status_code == 401


def test_seeded_credentials_fail_even_when_other_users_exist(anon_client):
    r = anon_client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin123"})
    assert r.status_code == 401


def test_pbkdf2_parameters_and_stored_iteration_count():
    a, b = hash_password("same-password"), hash_password("same-password")
    assert a != b  # per-hash random salt
    ia, sa, _ = parse_hash(a)  # type: ignore[misc]
    ib, sb, _ = parse_hash(b)  # type: ignore[misc]
    assert ia == ib >= 600_000 and sa != sb and len(sa) >= 16
    legacy = hash_password("pw", iterations=1234)  # lower stored count still verifies with ITS count
    assert legacy.startswith("pbkdf2_sha256$1234$")
    assert verify_password("pw", legacy) and not verify_password("px", legacy)
    assert not verify_password("pw", "pbkdf2_sha256$0$aa$bb")
    assert not verify_password("pw", "garbage")


def test_verify_uses_constant_time_compare(monkeypatch):
    calls = []
    real = am.hmac.compare_digest
    monkeypatch.setattr(am.hmac, "compare_digest", lambda x, y: calls.append(1) or real(x, y))
    verify_password("pw", hash_password("pw", iterations=1000))
    assert calls


def test_unknown_user_still_runs_a_full_cost_hash(monkeypatch):
    seen: list[str] = []
    real = am.verify_password
    monkeypatch.setattr(am, "verify_password", lambda pw, h: seen.append(h) or real(pw, h))
    mgr = AuthManager(users={})
    assert mgr.authenticate("nobody", "whatever") is None
    assert len(seen) == 1
    iterations, _, _ = parse_hash(seen[0])  # type: ignore[misc]
    assert iterations >= 600_000


def test_users_load_from_env_hashes(strong_hash):
    env = {"ADMIN_PASSWORD_HASH": strong_hash, "COGNI_AUDITOR_PASSWORD_HASH": strong_hash}
    users = load_users_from_env(env)
    assert set(users) == {"admin", "auditor"} and users["admin"].roles[0] == "admin"
    assert AuthManager(users=users).authenticate("Admin", "a-very-long-test-password") is not None
    with pytest.raises(AuthConfigError):
        load_users_from_env({"ADMIN_PASSWORD_HASH": hash_password("x", iterations=10)})


def test_bootstrap_cli_writes_a_loadable_hash(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(bootstrap.getpass, "getpass", lambda prompt="": "bootstrap-test-password")
    env_file = tmp_path / ".env"
    assert bootstrap.main(["--role", "operator", "--env-file", str(env_file)]) == 0
    text = env_file.read_text(encoding="utf-8")
    assert "bootstrap-test-password" not in text and "bootstrap-test-password" not in capsys.readouterr().out
    value = text.split("=", 1)[1].strip().strip("'")
    users = load_users_from_env({"OPERATOR_PASSWORD_HASH": value})
    assert AuthManager(users=users).authenticate("operator", "bootstrap-test-password") is not None
    assert bootstrap.main(["--role", "operator", "--env-file", str(env_file)]) == 1  # refuses to overwrite


# ---- 4. session ownership --------------------------------------------------------------------------
def _own(client, headers, sid):
    r = client.post("/api/v1/memory/sessions", json={"session_id": sid}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def test_cross_user_session_reads_are_403(anon_client, test_passwords, second_operator, app):
    op = _login(anon_client, "operator", test_passwords["operator"])
    op2 = _login(anon_client, "operator2", second_operator)
    aud = _login(anon_client, "auditor", test_passwords["auditor"])
    adm = _login(anon_client, "admin", test_passwords["admin"])
    assert _own(anon_client, op, "owned-by-op")["owner"] == "operator"
    app.state.container.memory.log("owned-by-op", "query", "private question")

    url = "/api/v1/memory/audit"
    assert anon_client.get(url, params={"session_id": "owned-by-op"}, headers=op).status_code == 200
    assert anon_client.get(url, params={"session_id": "owned-by-op"}, headers=op2).status_code == 403
    assert anon_client.get("/api/v1/memory/traces", params={"session_id": "owned-by-op"}, headers=op2).status_code == 403
    for elevated in (aud, adm):
        r = anon_client.get(url, params={"session_id": "owned-by-op"}, headers=elevated)
        assert r.status_code == 200 and r.json()["records"]
    # unknown / unowned ids are not readable by ordinary users either
    assert anon_client.get(url, params={"session_id": "never-created"}, headers=op2).status_code == 403
    # global listing is elevated-only
    assert anon_client.get(url, headers=op).status_code == 403
    assert anon_client.get(url, headers=aud).status_code == 200


def test_session_listing_is_scoped_by_owner(anon_client, test_passwords, second_operator):
    op = _login(anon_client, "operator", test_passwords["operator"])
    op2 = _login(anon_client, "operator2", second_operator)
    aud = _login(anon_client, "auditor", test_passwords["auditor"])
    _own(anon_client, op, "list-op")
    _own(anon_client, op2, "list-op2")

    def ids(h):
        return {s["session_id"] for s in anon_client.get("/api/v1/memory/sessions", headers=h).json()["sessions"]}

    assert ids(op) == {"list-op"} and ids(op2) == {"list-op2"}
    assert {"list-op", "list-op2"} <= ids(aud)
    assert "security" not in ids(aud)


def test_security_session_is_admin_auditor_only(anon_client, test_passwords):
    op = _login(anon_client, "operator", test_passwords["operator"])
    aud = _login(anon_client, "auditor", test_passwords["auditor"])
    adm = _login(anon_client, "admin", test_passwords["admin"])
    anon_client.post("/api/v1/auth/login", json={"username": "x", "password": "y"})  # writes a security record
    url, q = "/api/v1/memory/audit", {"session_id": "security"}
    assert anon_client.get(url, params=q, headers=op).status_code == 403
    for h in (aud, adm):
        r = anon_client.get(url, params=q, headers=h)
        assert r.status_code == 200 and r.json()["records"]


def test_delete_session_semantics(anon_client, test_passwords, second_operator, app):
    op = _login(anon_client, "operator", test_passwords["operator"])
    op2 = _login(anon_client, "operator2", second_operator)
    adm = _login(anon_client, "admin", test_passwords["admin"])
    d = "/api/v1/memory/sessions/"
    assert anon_client.delete(d + "does-not-exist", headers=op).status_code == 404  # TST-06
    assert anon_client.delete(d + "bad id!", headers=op).status_code == 422
    assert anon_client.delete(d + "security", headers=adm).status_code == 404
    _own(anon_client, op, "del-me")
    app.state.container.memory.log("del-me", "query", "q")
    assert anon_client.delete(d + "del-me", headers=op2).status_code == 404  # not the owner: existence hidden
    assert anon_client.delete(d + "del-me", headers=op).status_code == 200
    sessions = anon_client.get("/api/v1/memory/sessions", headers=adm).json()["sessions"]
    assert "del-me" not in {s["session_id"] for s in sessions}  # REV-27: no longer listed from the audit
    assert anon_client.delete(d + "del-me", headers=op).status_code == 404
    _own(anon_client, op, "admin-del")
    assert anon_client.delete(d + "admin-del", headers=adm).status_code == 200  # admin override


def test_create_session_body_is_honoured_or_rejected(anon_client, test_passwords, second_operator):
    op = _login(anon_client, "operator", test_passwords["operator"])
    op2 = _login(anon_client, "operator2", second_operator)
    u = "/api/v1/memory/sessions"
    assert anon_client.post(u, json={"session_id": "chosen-id"}, headers=op).json()["session_id"] == "chosen-id"
    assert anon_client.post(u, json={"session_id": "chosen-id"}, headers=op).status_code == 200  # idempotent
    assert anon_client.post(u, json={"session_id": "chosen-id"}, headers=op2).status_code == 409
    assert anon_client.post(u, json={"session_id": "../etc"}, headers=op).status_code == 422
    assert anon_client.post(u, json={"session_id": "security"}, headers=op).status_code == 422
    generated = anon_client.post(u, headers=op).json()
    assert re.fullmatch(r"[0-9a-f]{12}", generated["session_id"]) and generated["owner"] == "operator"


def test_traces_are_scoped_to_session_owner(anon_client, test_passwords, second_operator):
    op = _login(anon_client, "operator", test_passwords["operator"])
    op2 = _login(anon_client, "operator2", second_operator)
    aud = _login(anon_client, "auditor", test_passwords["auditor"])
    _own(anon_client, op, "trace-sess")
    run = anon_client.post("/api/v1/agent/run", json={"query": "calculate 2+2", "session_id": "trace-sess"}, headers=op)
    assert run.status_code == 200
    tid = run.json()["trace_id"]
    t = "/api/v1/telemetry/traces"
    assert tid in {x["id"] for x in anon_client.get(t, headers=op).json()}
    assert tid not in {x["id"] for x in anon_client.get(t, headers=op2).json()}
    assert tid in {x["id"] for x in anon_client.get(t, headers=aud).json()}


def test_authorize_session_helper_covers_the_run_path(app):
    from fastapi import HTTPException

    from nexus_engine.schemas.auth import AuthenticatedUser

    c = app.state.container
    alice = AuthenticatedUser(username="alice", roles=["operator"])
    bob = AuthenticatedUser(username="bob", roles=["operator"])
    root = AuthenticatedUser(username="root", roles=["admin"])
    deps.authorize_session(c, alice, "fresh-run", claim=True)  # first run binds the session
    deps.authorize_session(c, alice, "fresh-run", claim=True)
    with pytest.raises(HTTPException) as ei:
        deps.authorize_session(c, bob, "fresh-run", claim=True)
    assert ei.value.status_code == 403
    deps.authorize_session(c, root, "fresh-run", claim=True)
    with pytest.raises(HTTPException):
        deps.authorize_session(c, bob, "security")


def test_public_viewer_has_no_data_route_access(app):
    """Even a forged session with only the old anonymous role is refused on every data route."""
    sess = app.state.container.auth_mgr.create_session("anon-ish", ["public_viewer"])
    c = TestClient(app, headers={"Authorization": f"Bearer {sess.token}"})
    for method, path in PROTECTED[:4]:
        assert c.request(method, path).status_code == 403, path


# ---- 5. /health ---------------------------------------------------------------------------------------
def test_health_is_liveness_only_when_anonymous(anon_client):
    for path in ("/health", "/api/v1/health"):
        r = anon_client.get(path)
        assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_health_details_requires_admin_or_auditor(anon_client, test_passwords):
    for path in ("/health/details", "/api/v1/health/details"):
        assert anon_client.get(path).status_code == 401
        assert anon_client.get(path, headers=_login(anon_client, "operator", test_passwords["operator"])).status_code == 403
    r = anon_client.get("/api/v1/health/details", headers=_login(anon_client, "auditor", test_passwords["auditor"]))
    assert r.status_code == 200
    body = r.json()
    assert {"settings", "databases", "embedding_probe", "llm_probe", "version", "uptime_s"} <= set(body)


def test_health_details_leaks_no_paths_or_secrets(settings, tmp_path, make_client):
    cfg = replace(settings, gemini_api_key="sekrit-llm-key", gemini_embedding_api_key="sekrit-emb-key", api_key="sekrit-api")
    app = create_app(cfg)
    r = make_client(app).get("/api/v1/health/details")
    assert r.status_code == 200
    dumped = json.dumps(r.json())
    assert str(tmp_path) not in dumped and "sekrit" not in dumped
    assert all("path" not in db for db in r.json()["databases"])
    assert not any("path" in k for k in r.json()["settings"])


def test_health_probes_are_cached(client, monkeypatch):
    calls = {"emb": 0, "llm": 0}

    def emb(**_kw):
        calls["emb"] += 1
        return {"online": True}

    def llm(**_kw):
        calls["llm"] += 1
        return {"online": True}

    monkeypatch.setattr(health_module, "_probe_embedding_status", emb)
    monkeypatch.setattr(health_module, "_probe_llm_status", llm)
    for _ in range(3):
        assert client.get("/api/v1/health/details").status_code == 200
    assert calls == {"emb": 1, "llm": 1}


def test_probe_cache_expires_after_ttl():
    now = [0.0]
    cache = health_module.ProbeCache(clock=lambda: now[0])
    assert health_module.PROBE_CACHE_TTL_S >= 30
    n = [0]

    def produce():
        n[0] += 1
        return {"n": n[0]}

    assert cache.get(produce) == {"n": 1}
    now[0] = health_module.PROBE_CACHE_TTL_S - 1
    assert cache.get(produce) == {"n": 1}
    now[0] = health_module.PROBE_CACHE_TTL_S + 1
    assert cache.get(produce) == {"n": 2}


def test_settings_fixture_has_no_shared_key(settings: Settings):
    assert settings.api_key is None
