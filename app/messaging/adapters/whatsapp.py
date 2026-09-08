from __future__ import annotations

from typing import Any, Mapping

import httpx

from app.messaging.adapters import Channel, ChannelAdapter
from app.shared.config import MessagingConfig
from app.shared.exceptions import MessagingError


class WhatsAppAdapter(ChannelAdapter):
    """Meta Cloud API (Business). WhatsApp Web automation is not permitted."""

    name: Channel = "whatsapp"

    def __init__(self, config: MessagingConfig, client: httpx.Client | None = None) -> None:
        self._config = config
        self._owns = client is None
        self._http = client or httpx.Client(timeout=30.0)

    def close(self) -> None:
        if self._owns:
            self._http.close()

    def send(self, to: str, body: str, *, metadata: Mapping[str, Any] | None = None) -> str:
        if not self._config.whatsapp_token or not self._config.whatsapp_phone_number_id:
            raise MessagingError("WhatsApp credentials not configured")

        url = (
            f"{self._config.whatsapp_base_url.rstrip('/')}/"
            f"{self._config.whatsapp_phone_number_id}/messages"
        )
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": body},
        }
        response = self._http.post(
            url,
            headers={"Authorization": f"Bearer {self._config.whatsapp_token}"},
            json=payload,
        )
        if response.status_code >= 400:
            raise MessagingError(f"WhatsApp send failed ({response.status_code})")
        data = response.json()
        try:
            return str(data["messages"][0]["id"])
        except (KeyError, IndexError, TypeError) as exc:
            raise MessagingError("unexpected WhatsApp response") from exc
