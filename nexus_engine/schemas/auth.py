"""Authentication and RBAC schemas."""
from __future__ import annotations

import re
from pydantic import BaseModel, Field, field_validator

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)

    @field_validator("username")
    @classmethod
    def _clean_username(cls, v: str) -> str:
        cleaned = v.strip().lower()
        if not cleaned or not _USERNAME_RE.match(cleaned):
            raise ValueError("username must match [A-Za-z0-9_.-]{1,64}")
        return cleaned


class SessionInfo(BaseModel):
    username: str
    roles: list[str]


class LoginResponse(BaseModel):
    username: str
    roles: list[str]
    token: str


class AuthenticatedUser(BaseModel):
    username: str
    roles: list[str]
    auth_method: str = "session"
