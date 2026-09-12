from __future__ import annotations

from typing import Any, Mapping

from app.messaging.adapters import Channel, ChannelAdapter
from app.shared.config import MessagingConfig
from app.shared.exceptions import NookalCommunicationUnavailable


class SMSAdapter(ChannelAdapter):
    """
    Nookal-native SMS adapter.

    In the clinic's architecture:
    - Nookal is the communication provider.
    - No third-party SMS providers (Twilio, MessageBird, Vonage, AWS SNS) are permitted.
    - Nookal natively delivers automated appointment confirmations, reminders, and recalls
      configured in the Nookal PMS Admin UI (Manage > Communications).
    - The official Nookal API v2 does not expose an ad-hoc direct SMS sending endpoint.

    Attempting direct ad-hoc SMS dispatch raises NookalCommunicationUnavailable with
    a clear explanation so the dashboard does not falsely report messages as 'Sent'.
    """

    name: Channel = "sms"

    def __init__(self, config: MessagingConfig | None = None, client: Any = None) -> None:
        self._config = config
        self._client = client

    def close(self) -> None:
        pass

    def send(self, to: str, body: str, *, metadata: Mapping[str, Any] | None = None) -> str:
        """
        Direct SMS dispatch.

        Raises NookalCommunicationUnavailable because Nookal does not support
        ad-hoc direct SMS via API; automated appointment SMS is handled natively by Nookal.
        """
        raise NookalCommunicationUnavailable(
            "Direct SMS sending is not available through the configured Nookal API."
        )

