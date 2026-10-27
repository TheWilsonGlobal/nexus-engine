"""First-run credential bootstrap.

    python -m nexus_engine.services.security.bootstrap --role admin [--env-file .env]

Prompts (no echo) for a password, derives a PBKDF2-HMAC-SHA256 hash with a fresh random salt and
prints ``<ROLE>_PASSWORD_HASH='...'``. With ``--env-file`` the line is appended to that file
(refusing to overwrite an existing entry). Only the hash is ever printed or stored; the password
is never echoed, logged or written.

The hash contains ``$``. Keep it single-quoted in shell/.env files; when passing it through
docker compose interpolation, escape each ``$`` as ``$$``.
"""
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path
from typing import Optional, Sequence

from nexus_engine.services.security.auth_manager import USER_ENV_VARS, hash_password

MIN_PASSWORD_LENGTH = 12
_ROLE_TO_VAR = {username: var for var, (username, _roles) in USER_ENV_VARS.items()}


def _existing_vars(path: Path) -> set[str]:
    if not path.exists():
        return set()
    names: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            names.add(stripped.split("=", 1)[0].strip())
    return names


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Generate a PBKDF2 password hash for a built-in role.")
    parser.add_argument("--role", required=True, choices=sorted(_ROLE_TO_VAR))
    parser.add_argument("--env-file", type=Path, default=None, help="append the hash line to this file")
    args = parser.parse_args(argv)

    var = _ROLE_TO_VAR[args.role]
    password = getpass.getpass(f"Password for {args.role}: ")
    if len(password) < MIN_PASSWORD_LENGTH:
        print(f"error: password must be at least {MIN_PASSWORD_LENGTH} characters", file=sys.stderr)
        return 2
    if getpass.getpass("Repeat password: ") != password:
        print("error: passwords do not match", file=sys.stderr)
        return 2

    line = f"{var}='{hash_password(password)}'"
    if args.env_file is not None:
        if var in _existing_vars(args.env_file):
            print(f"error: {var} already set in {args.env_file}; remove it first to rotate", file=sys.stderr)
            return 1
        with args.env_file.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        print(f"{var} written to {args.env_file}")
    else:
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
