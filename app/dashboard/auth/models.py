"""Authenticated principal — identity only; authorization is separate."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class User:
    user_id: str
    username: str
    role: str
    display_name: str | None = None


@dataclass(frozen=True)
class Session:
    session_id: str
    user_id: str
    username: str
    role: str
    csrf_token: str
    created_at: str
    expires_at: str
