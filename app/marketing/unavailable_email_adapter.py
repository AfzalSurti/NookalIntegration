from __future__ import annotations

from typing import Any, Mapping


class UnavailableEmailAdapter:
    """
    Production placeholder when external email provider is not yet configured.
    Raises explicit NotImplementedError on send to prevent silent fake email sends in production.
    """

    def __init__(self, channel: str = "email") -> None:
        self.name = channel

    def send(self, to: str, body: str, *, metadata: Mapping[str, Any] | None = None) -> str:
        raise NotImplementedError(
            "UnavailableEmailAdapter: production email provider is not configured. "
            "Configure SMTP/API provider credentials before dispatching campaign emails."
        )
