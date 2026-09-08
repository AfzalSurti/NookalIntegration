from __future__ import annotations

from typing import Any, Mapping

import httpx

from app.messaging.adapters import Channel, ChannelAdapter
from app.shared.config import MessagingConfig
from app.shared.exceptions import MessagingError


class SMSAdapter(ChannelAdapter):
    name: Channel = "sms"

    def __init__(self, config: MessagingConfig, client: httpx.Client | None = None) -> None:
        self._config = config
        self._owns = client is None
        self._http = client or httpx.Client(timeout=30.0)

    def close(self) -> None:
        if self._owns:
            self._http.close()

    def send(self, to: str, body: str, *, metadata: Mapping[str, Any] | None = None) -> str:
        # TODO: bind to the clinic's chosen SMS provider once account + sender ID exist.
        if not self._config.sms_api_key or not self._config.sms_base_url:
            raise MessagingError("SMS provider not configured")
        raise NotImplementedError(
            "SMSAdapter.send: implement against the contracted SMS provider docs"
        )
