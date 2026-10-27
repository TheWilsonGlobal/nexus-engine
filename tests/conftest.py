import dataclasses
import ipaddress
import os
import socket

import pytest

from nexus_engine.core.config import Settings
from nexus_engine.core.container import Container, build_container
from nexus_engine.main import create_app


_ISOLATED_ENV = ("EMBEDDING_PROVIDER", "LLM_PROVIDER", "DEFAULT_DATABASE_PATH", "DEFAULT_AUDIT_DB_PATH", "ENV")


@pytest.fixture(autouse=True, scope="session")
def setup_test_env(tmp_path_factory):
    """Offline mock providers, and DB paths redirected to a scratch dir.

    Tests build apps from the `settings` fixture (tmp_path). The DB-path override is a backstop so that a stray
    `Settings.from_env()` / `create_app()` can never open the real data/db/*.db.
    """
    saved = {k: os.environ.get(k) for k in _ISOLATED_ENV}
    scratch = tmp_path_factory.mktemp("nexus-session-db")
    os.environ["EMBEDDING_PROVIDER"] = "mock"
    os.environ["LLM_PROVIDER"] = "mock"
    os.environ["ENV"] = "development"  # open mode / no env password hashes are only legal in development
    os.environ["DEFAULT_DATABASE_PATH"] = str(scratch / "nexus.db")
    os.environ["DEFAULT_AUDIT_DB_PATH"] = str(scratch / "audit.db")
    yield
    for k, v in saved.items():
        if v is not None:
            os.environ[k] = v
        else:
            os.environ.pop(k, None)


@pytest.fixture()
def settings(tmp_path) -> Settings:
    return Settings(
        audit_db_path=str(tmp_path / "audit.db"),
        database_path=str(tmp_path / "nexus.db"),
        cors_origins=("http://127.0.0.1:5200", "http://localhost:5200", "http://127.0.0.1:5199", "http://localhost:5199"),
        sandbox_timeout_s=3.0,
        sandbox_memory_mb=128,
        llm_provider="mock",
        embedding_provider="mock",
        network_enabled=True,
    )


@pytest.fixture()
def container(settings: Settings) -> Container:
    return build_container(settings)


@pytest.fixture()
def sandbox_settings(settings: Settings) -> Settings:
    """`settings` with the (off-by-default) sandbox switched on. The policy still applies per call."""
    return dataclasses.replace(settings, sandbox_enabled=True)


@pytest.fixture()
def sandbox_container(sandbox_settings: Settings) -> Container:
    return build_container(sandbox_settings)


@pytest.fixture()
def as_admin():
    """Factory: context manager binding an admin principal whose own message is `query` (what the API layer
    does around an agent run). Code is only accepted when it comes from that message (SEC-05)."""
    from nexus_engine.services.agent.planner import extract_code
    from nexus_engine.services.sandbox.policy import Principal, bind_principal

    def _bind(query: str):
        return bind_principal(Principal("root", frozenset({"admin"}), query, extract_code(query)))

    return _bind


# Test users are provisioned through the AuthManager (no credentials exist in source or env).
# Low PBKDF2 iteration count keeps login-heavy tests fast; verify_password honours the stored count.
TEST_PASSWORDS = {
    "admin": "t3st-Admin-pw-1",
    "operator": "t3st-Operator-pw-2",
    "auditor": "t3st-Auditor-pw-3",
}
_TEST_ROLES = {
    "admin": ["admin", "lead_engineer"],
    "operator": ["operator", "analyst"],
    "auditor": ["auditor", "read_only"],
}


@pytest.fixture()
def test_passwords() -> dict:
    return dict(TEST_PASSWORDS)


@pytest.fixture()
def app(settings: Settings):
    from nexus_engine.services.security.auth_manager import hash_password

    application = create_app(settings)
    for name, pw in TEST_PASSWORDS.items():
        application.state.container.auth_mgr.add_user(name, hash_password(pw, iterations=1000), _TEST_ROLES[name])
    return application


@pytest.fixture()
def sandbox_client(sandbox_settings: Settings):
    """Admin-authenticated client against an app whose sandbox is enabled."""
    from fastapi.testclient import TestClient

    application = create_app(sandbox_settings)
    sess = application.state.container.auth_mgr.create_session("fixture_admin", ["admin"])
    return TestClient(application, client=("127.0.0.1", 50000), headers={"Authorization": f"Bearer {sess.token}"})


@pytest.fixture()
def anon_client(app):
    """A client with NO credentials."""
    from fastapi.testclient import TestClient

    return TestClient(app, client=("127.0.0.1", 50000))


@pytest.fixture()
def client(app):
    """A client pre-authenticated as an admin session (default Authorization header; per-request
    headers override it). Use `anon_client` to test unauthenticated behaviour."""
    from fastapi.testclient import TestClient

    sess = app.state.container.auth_mgr.create_session("fixture_admin", ["admin"])
    return TestClient(
        app, client=("127.0.0.1", 50000), headers={"Authorization": f"Bearer {sess.token}"}
    )


@pytest.fixture()
def make_client():
    """Factory: an admin-authenticated TestClient for an app a test built itself."""
    from fastapi.testclient import TestClient

    def _make(application, **kwargs):
        sess = application.state.container.auth_mgr.create_session("fixture_admin", ["admin"])
        return TestClient(application, headers={"Authorization": f"Bearer {sess.token}"}, **kwargs)

    return _make


def _is_loopback(host) -> bool:
    if isinstance(host, bytes):
        host = host.decode("ascii", "ignore")
    if not isinstance(host, str):
        return True
    if host in ("localhost", "", "<broadcast>") or host.startswith("localhost"):
        return host != "<broadcast>"
    try:
        return ipaddress.ip_address(host.split("%")[0]).is_loopback
    except ValueError:
        return False


@pytest.fixture(autouse=True, scope="session")
def block_outbound_network():
    """Fail fast on any non-loopback socket connection unless NEXUS_LIVE_TESTS=1."""
    if os.environ.get("NEXUS_LIVE_TESTS") == "1":
        yield
        return

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo

    def _check(address):
        # AF_UNIX addresses are str paths / AF_INET are (host, port) tuples
        if isinstance(address, tuple) and address:
            host = address[0]
            if not _is_loopback(host):
                raise RuntimeError(
                    f"Outbound network blocked during tests (host={host!r}). "
                    "Set NEXUS_LIVE_TESTS=1 to allow live calls."
                )

    def guarded_connect(self, address):
        _check(address)
        return real_connect(self, address)

    def guarded_connect_ex(self, address):
        _check(address)
        return real_connect_ex(self, address)

    def guarded_getaddrinfo(host, *args, **kwargs):
        if not _is_loopback(host):
            raise RuntimeError(
                f"Outbound DNS lookup blocked during tests (host={host!r}). "
                "Set NEXUS_LIVE_TESTS=1 to allow live calls."
            )
        return real_getaddrinfo(host, *args, **kwargs)

    socket.socket.connect = guarded_connect
    socket.socket.connect_ex = guarded_connect_ex
    socket.getaddrinfo = guarded_getaddrinfo
    try:
        yield
    finally:
        socket.socket.connect = real_connect
        socket.socket.connect_ex = real_connect_ex
        socket.getaddrinfo = real_getaddrinfo
