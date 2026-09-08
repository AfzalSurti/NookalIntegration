from __future__ import annotations

from typing import Any, Mapping

from app.messaging.adapters import Channel, ChannelAdapter
from app.shared.config import MessagingConfig
from app.shared.exceptions import MessagingError


class EmailAdapter(ChannelAdapter):
    name: Channel = "email"

    def __init__(self, config: MessagingConfig) -> None:
        self._config = config

    def send(self, to: str, body: str, *, metadata: Mapping[str, Any] | None = None) -> str:
        # TODO: SMTP or provider API once credentials are confirmed.
        if not self._config.smtp_host:
            raise MessagingError("email/SMTP not configured")
        raise NotImplementedError(
            "EmailAdapter.send: implement against SMTP/API docs (do not guess auth flow)"
        )
