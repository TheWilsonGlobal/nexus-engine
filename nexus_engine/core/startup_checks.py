"""Fail-fast security checks run by ``create_app`` before any service is built."""
from __future__ import annotations

import ipaddress
from typing import Mapping, Optional

from nexus_engine.core.config import Settings
from nexus_engine.services.security.auth_manager import (
    AuthConfigError,
    env_lookup,
    load_users_from_env,
    verify_password,
)

DEV_ENVS = frozenset({"development", "dev"})
# Passwords that shipped as built-in defaults in earlier versions; a hash of any of them is refused.
_RETIRED_DEFAULT_PASSWORDS = {"admin": "admin123", "operator": "operator123", "auditor": "audit123"}


class StartupCheckError(RuntimeError):
    """The configuration is not safe to serve requests with."""


def is_development(env: Optional[Mapping[str, str]] = None) -> bool:
    """True only when ENV is explicitly set to development. Unset means production."""
    return (env_lookup(env, "ENV") or "").lower() in DEV_ENVS


def is_loopback_host(host: str) -> bool:
    host = (host or "").strip().strip("[]").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def run_startup_checks(settings: Settings, env: Optional[Mapping[str, str]] = None) -> None:
    """Raise StartupCheckError on an unsafe configuration.

    1. Open mode (no API_KEY) is only allowed when ENV=development AND the bind host is loopback.
    2. Outside development, an admin password hash must be supplied (ADMIN_PASSWORD_HASH).
    3. Configured hashes must be well formed, strong, and not a retired built-in default password.
    """
    dev = is_development(env)

    if not settings.api_key:
        if not dev:
            raise StartupCheckError(
                "API_KEY is not set: open mode is only allowed with ENV=development. "
                "Set API_KEY (or ENV=development for local use)."
            )
        if not is_loopback_host(settings.backend_host):
            raise StartupCheckError(
                f"API_KEY is not set and BACKEND_HOST={settings.backend_host!r} is not a loopback address: "
                "open mode is only allowed when bound to 127.0.0.1/::1/localhost."
            )

    try:
        users = load_users_from_env(env)
    except AuthConfigError as exc:
        raise StartupCheckError(str(exc)) from exc

    if not dev and "admin" not in users:
        raise StartupCheckError(
            "ADMIN_PASSWORD_HASH is not set. Generate one with "
            "'python -m nexus_engine.services.security.bootstrap --role admin'."
        )

    for name, rec in users.items():
        retired = _RETIRED_DEFAULT_PASSWORDS.get(name)
        if retired and verify_password(retired, rec.password_hash):
            raise StartupCheckError(f"{name} password hash matches a retired default password; choose a new one")
