from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Literal, Mapping

Channel = Literal["whatsapp", "sms", "email"]


class ChannelAdapter(ABC):
    name: Channel

    @abstractmethod
    def send(self, to: str, body: str, *, metadata: Mapping[str, Any] | None = None) -> str:
        """Return provider message id."""
