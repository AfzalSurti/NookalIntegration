"""
Controllable messaging adapter for tests.

Simulates success, failure, and timeout without network I/O.
Implements the same ChannelAdapter interface as production adapters.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from app.messaging.adapters import Channel, ChannelAdapter
from app.shared.exceptions import MessagingError


class FakeSendMode(str, Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    TIMEOUT = "timeout"


class FakeTimeoutError(MessagingError):
    """Deterministic stand-in for a provider timeout (no real network wait)."""


@dataclass
class CapturedSend:
    to: str
    body: str
    metadata: dict[str, Any]
    mode: FakeSendMode
    provider_ref: str | None = None


class FakeAdapter(ChannelAdapter):
    """
    Richer than StubAdapter: mode can flip between success/failure/timeout,
    and every attempt is recorded in `sent` (including failed attempts).
    """

    def __init__(
        self,
        name: Channel = "whatsapp",
        *,
        mode: FakeSendMode = FakeSendMode.SUCCESS,
    ) -> None:
        self.name = name
        self.mode = mode
        self.sent: list[CapturedSend] = []
        self.attempt_count = 0

    def set_mode(self, mode: FakeSendMode) -> None:
        self.mode = mode

    def reset(self) -> None:
        self.sent.clear()
        self.attempt_count = 0

    def send(self, to: str, body: str, *, metadata: Mapping[str, Any] | None = None) -> str:
        self.attempt_count += 1
        meta = dict(metadata or {})
        if self.mode == FakeSendMode.FAILURE:
            self.sent.append(
                CapturedSend(to=to, body=body, metadata=meta, mode=self.mode)
            )
            raise MessagingError(f"fake {self.name} provider failure")
        if self.mode == FakeSendMode.TIMEOUT:
            self.sent.append(
                CapturedSend(to=to, body=body, metadata=meta, mode=self.mode)
            )
            raise FakeTimeoutError(f"fake {self.name} provider timeout")

        ref = f"fake_{self.name}_{uuid.uuid4().hex[:10]}"
        self.sent.append(
            CapturedSend(
                to=to,
                body=body,
                metadata=meta,
                mode=self.mode,
                provider_ref=ref,
            )
        )
        return ref
