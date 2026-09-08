from __future__ import annotations

import uuid
from typing import Any, Mapping


class FakeEmailAdapter:
    """Offline email transport for marketing campaigns."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.sent_count = 0

    def send(self, to: str, body: str, *, metadata: Mapping[str, Any] | None = None) -> str:
        ref = f"fake-email-{uuid.uuid4().hex[:12]}"
        self.sent.append(
            {
                "to": to,
                "body": body,
                "metadata": dict(metadata or {}),
                "ref": ref,
            }
        )
        self.sent_count += 1
        return ref
