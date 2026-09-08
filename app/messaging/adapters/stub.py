from __future__ import annotations

import uuid
from typing import Any, Mapping

from app.messaging.adapters import Channel, ChannelAdapter


class StubAdapter(ChannelAdapter):
    """Records sends without hitting a network — used until credentials land."""

    def __init__(self, name: Channel) -> None:
        self.name = name
        self.sent: list[dict[str, Any]] = []

    def send(self, to: str, body: str, *, metadata: Mapping[str, Any] | None = None) -> str:
        ref = f"stub_{self.name}_{uuid.uuid4().hex[:10]}"
        self.sent.append(
            {"to": to, "body": body, "metadata": dict(metadata or {}), "ref": ref}
        )
        return ref
