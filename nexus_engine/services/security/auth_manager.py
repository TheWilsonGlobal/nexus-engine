"""Cryptographic password hashing (PBKDF2-HMAC-SHA256) and session lifecycle manager."""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Callable, Mapping, Optional

PBKDF2_ALGO = "pbkdf2_sha256"
PBKDF2_ITERATIONS = 600_000  # OWASP 2023 minimum for PBKDF2-HMAC-SHA256
MIN_ENV_HASH_ITERATIONS = PBKDF2_ITERATIONS
SALT_BYTES = 16

# Environment variable (resolved with the usual NEXUS_/COGNI_ prefixes) -> (username, roles).
USER_ENV_VARS: dict[str, tuple[str, tuple[str, ...]]] = {
    "ADMIN_PASSWORD_HASH": ("admin", ("admin", "lead_engineer")),
    "OPERATOR_PASSWORD_HASH": ("operator", ("operator", "analyst")),
    "AUDITOR_PASSWORD_HASH": ("auditor", ("auditor", "read_only")),
}


def hash_password(password: str, salt: Optional[bytes] = None, iterations: int = PBKDF2_ITERATIONS) -> str:
    """Hash a password with PBKDF2-HMAC-SHA256 and a per-call random salt.

    Format: ``pbkdf2_sha256$<iterations>$<salt hex>$<hash hex>``; the iteration count is stored so
    it can be raised later without invalidating existing hashes.
    """
    raw_salt = salt if salt is not None else secrets.token_bytes(SALT_BYTES)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), raw_salt, iterations)
    return f"{PBKDF2_ALGO}${iterations}${raw_salt.hex()}${dk.hex()}"


def parse_hash(stored_hash: str) -> Optional[tuple[int, bytes, bytes]]:
    """Return (iterations, salt, digest) or None if ``stored_hash`` is not a valid pbkdf2_sha256 hash."""
    try:
        parts = stored_hash.split("$")
        if len(parts) != 4 or parts[0] != PBKDF2_ALGO:
            return None
        iterations = int(parts[1])
        raw_salt = bytes.fromhex(parts[2])
        expected_dk = bytes.fromhex(parts[3])
    except (ValueError, TypeError, AttributeError):
        return None
    if iterations < 1 or not raw_salt or len(expected_dk) != 32:
        return None
    return iterations, raw_salt, expected_dk


def verify_password(password: str, stored_hash: str) -> bool:
    """Constant-time verification of a password against a stored PBKDF2 hash (uses the stored count)."""
    parsed = parse_hash(stored_hash)
    if parsed is None:
        return False
    iterations, raw_salt, expected_dk = parsed
    computed_dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), raw_salt, iterations)
    return hmac.compare_digest(computed_dk, expected_dk)


@dataclass
class UserRecord:
    username: str
    password_hash: str
    roles: list[str]


@dataclass
class SessionRecord:
    token: str
    username: str
    roles: list[str]
    created_at: float
    last_active_at: float


class AuthConfigError(RuntimeError):
    """Raised when configured credentials are missing or unacceptable."""


def load_users_from_env(env: Optional[Mapping[str, str]] = None) -> dict[str, UserRecord]:
    """Build the user table from ``*_PASSWORD_HASH`` variables. No credentials are ever built in.

    Each value must be a ``pbkdf2_sha256$iters$salt$hash`` string with at least
    ``MIN_ENV_HASH_ITERATIONS`` iterations. Unset variables simply mean the user does not exist.
    """
    users: dict[str, UserRecord] = {}
    for var, (username, roles) in USER_ENV_VARS.items():
        raw = env_lookup(env, var)
        if not raw:
            continue
        parsed = parse_hash(raw)
        if parsed is None:
            raise AuthConfigError(f"{var} is not a valid '{PBKDF2_ALGO}$iterations$salt_hex$hash_hex' hash")
        if parsed[0] < MIN_ENV_HASH_ITERATIONS:
            raise AuthConfigError(f"{var} uses {parsed[0]} PBKDF2 iterations; at least {MIN_ENV_HASH_ITERATIONS} required")
        users[username] = UserRecord(username=username, password_hash=raw, roles=list(roles))
    return users


def env_lookup(env: Optional[Mapping[str, str]], name: str) -> Optional[str]:
    source = os.environ if env is None else env
    for key in (name, f"NEXUS_{name}", f"COGNI_{name}"):
        val = source.get(key)
        if val is not None and val.strip():
            return val.strip()
    return None


_DUMMY_HASH: Optional[str] = None
_DUMMY_LOCK = threading.Lock()


def _dummy_hash() -> str:
    """Random per-process hash at the production cost, used to equalise timing for unknown users."""
    global _DUMMY_HASH
    with _DUMMY_LOCK:
        if _DUMMY_HASH is None:
            _DUMMY_HASH = hash_password(secrets.token_urlsafe(16))
        return _DUMMY_HASH


class AuthManager:
    """Thread-safe user authentication and session manager with TTL and idle expiration."""

    def __init__(
        self,
        ttl_s: float = 86400.0,
        idle_timeout_s: float = 7200.0,
        clock: Callable[[], float] = time.time,
        session_ttl_s: Optional[float] = None,
        session_idle_s: Optional[float] = None,
        users: Optional[Mapping[str, UserRecord]] = None,
    ) -> None:
        self.ttl_s = session_ttl_s if session_ttl_s is not None else ttl_s
        self.idle_timeout_s = session_idle_s if session_idle_s is not None else idle_timeout_s
        self._clock = clock
        self._lock = threading.Lock()
        self._users: dict[str, UserRecord] = dict(users) if users is not None else load_users_from_env()
        self._sessions: dict[str, SessionRecord] = {}

    def add_user(self, username: str, password_hash: str, roles: list[str]) -> UserRecord:
        """Register a user from an existing hash (used by tests and provisioning code)."""
        if parse_hash(password_hash) is None:
            raise AuthConfigError("password_hash is not a valid pbkdf2_sha256 hash")
        role_list = [roles] if isinstance(roles, str) else list(roles)
        rec = UserRecord(username=username.strip().lower(), password_hash=password_hash, roles=role_list)
        with self._lock:
            self._users[rec.username] = rec
        return rec

    def has_users(self) -> bool:
        with self._lock:
            return bool(self._users)

    def authenticate(self, username: str, password: str) -> Optional[UserRecord]:
        clean_user = username.strip().lower()
        with self._lock:
            user = self._users.get(clean_user)
        if user is None:
            # Mitigate user-enumeration timing attacks by running a dummy PBKDF2
            verify_password(password, _dummy_hash())
            return None
        if not verify_password(password, user.password_hash):
            return None
        return user

    def create_session(self, username: str, roles: list[str]) -> SessionRecord:
        now = self._clock()
        token = secrets.token_urlsafe(32)
        rec = SessionRecord(
            token=token,
            username=username,
            roles=list(roles),
            created_at=now,
            last_active_at=now,
        )
        with self._lock:
            self._prune_locked(now)
            self._sessions[token] = rec
        return rec

    def get_session(self, token: str) -> Optional[SessionRecord]:
        if not token:
            return None
        now = self._clock()
        with self._lock:
            self._prune_locked(now)
            rec = self._sessions.get(token)
            if rec is None:
                return None
            rec.last_active_at = now
            return rec

    def revoke_session(self, token: str) -> bool:
        if not token:
            return False
        with self._lock:
            return self._sessions.pop(token, None) is not None

    verify_session = get_session

    def _prune_locked(self, now: float) -> None:
        expired = [
            tok
            for tok, s in self._sessions.items()
            if (now - s.created_at > self.ttl_s) or (now - s.last_active_at > self.idle_timeout_s)
        ]
        for tok in expired:
            self._sessions.pop(tok, None)
