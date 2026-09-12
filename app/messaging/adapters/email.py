from __future__ import annotations

from typing import Any, Mapping

from app.messaging.adapters import Channel, ChannelAdapter
from app.shared.config import MessagingConfig
from app.shared.exceptions import NookalCommunicationUnavailable


class EmailAdapter(ChannelAdapter):
    """
    Nookal-native Email adapter.

    In the clinic's architecture:
    - Nookal is the communication provider.
    - No third-party email providers (SendGrid, Mailgun, external SMTP) are permitted.
    - Nookal natively delivers automated appointment emails, confirmations, and reminders
      configured in the Nookal PMS Admin UI (Manage > Communications).
    - The official Nookal API v2 does not expose an ad-hoc direct email sending endpoint.

    Attempting direct ad-hoc email dispatch raises NookalCommunicationUnavailable with
    a clear explanation so the dashboard does not falsely report messages as 'Sent'.
    """

    name: Channel = "email"

    def __init__(self, config: MessagingConfig | None = None, client: Any = None) -> None:
        self._config = config
        self._client = client

    def close(self) -> None:
        pass

    def send(self, to: str, body: str, *, metadata: Mapping[str, Any] | None = None) -> str:
        """
        Direct Email dispatch.

        Raises NookalCommunicationUnavailable because Nookal does not support
        ad-hoc direct email via API; automated appointment email is handled natively by Nookal.
        """
        raise NookalCommunicationUnavailable(
            "Direct Email sending is not available through the configured Nookal API."
        )

