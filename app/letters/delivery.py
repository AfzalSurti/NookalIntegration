"""Document delivery abstraction — Phase D uses in-memory fake."""
from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from app.letters.models import DocumentRecord
from app.shared.exceptions import KillSwitchActive
from app.shared.kill_switch import assert_allows


@dataclass
class DeliveryRecord:
    document_id: str
    destination: str
    channel: str
    metadata: dict[str, Any] = field(default_factory=dict)


class DocumentDelivery(ABC):
    @abstractmethod
    def deliver(
        self,
        document: DocumentRecord,
        *,
        destination: str,
        channel: str = "store",
        metadata: dict[str, Any] | None = None,
    ) -> DeliveryRecord:
        ...


class InMemoryDocumentDelivery(DocumentDelivery):
    def __init__(self, *, respect_kill_switch: bool = True) -> None:
        self._lock = threading.Lock()
        self.sent: list[DeliveryRecord] = []
        self._respect_kill_switch = respect_kill_switch

    def deliver(
        self,
        document: DocumentRecord,
        *,
        destination: str,
        channel: str = "store",
        metadata: dict[str, Any] | None = None,
    ) -> DeliveryRecord:
        if self._respect_kill_switch:
            assert_allows(f"document.deliver.{channel}")
        rec = DeliveryRecord(
            document_id=document.document_id,
            destination=destination,
            channel=channel,
            metadata=dict(metadata or {}),
        )
        with self._lock:
            self.sent.append(rec)
        return rec


class FileSystemDocumentDelivery(DocumentDelivery):
    """Production filesystem-backed document delivery logger."""

    def __init__(self, directory: Path | str, *, respect_kill_switch: bool = True) -> None:
        import json
        from pathlib import Path
        self._json = json
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._log_file = self._dir / "deliveries.jsonl"
        self._lock = threading.Lock()
        self._respect_kill_switch = respect_kill_switch

    def deliver(
        self,
        document: DocumentRecord,
        *,
        destination: str,
        channel: str = "store",
        metadata: dict[str, Any] | None = None,
    ) -> DeliveryRecord:
        if self._respect_kill_switch:
            assert_allows(f"document.deliver.{channel}")
        rec = DeliveryRecord(
            document_id=document.document_id,
            destination=destination,
            channel=channel,
            metadata=dict(metadata or {}),
        )
        line = self._json.dumps({
            "document_id": rec.document_id,
            "destination": rec.destination,
            "channel": rec.channel,
            "metadata": rec.metadata,
        })
        with self._lock:
            with self._log_file.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        return rec

