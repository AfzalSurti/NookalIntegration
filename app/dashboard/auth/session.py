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
        uid = self._by_username.get(username.casefold())
        if uid is None:
            # Constant-ish work to avoid trivial username enumeration timing.
            verify_password(password, hash_password("dummy"))
            return None
        stored = self._users[uid]
        if not verify_password(password, stored.password_hash):
            return None
        return stored.user

    def get_user(self, user_id: str) -> User | None:
        stored = self._users.get(user_id)
        return stored.user if stored else None


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
