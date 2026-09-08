"""Document storage abstraction — Phase D uses in-memory fake."""
from __future__ import annotations

import threading
import uuid
from abc import ABC, abstractmethod
from typing import Any

from app.letters.models import DocumentRecord


class DocumentStore(ABC):
    @abstractmethod
    def save(self, record: DocumentRecord, content: bytes | None = None) -> DocumentRecord:
        ...

    @abstractmethod
    def retrieve(self, document_id: str) -> DocumentRecord | None:
        ...

    @abstractmethod
    def get_content(self, document_id: str) -> bytes | None:
        ...

    @abstractmethod
    def metadata(self, document_id: str) -> dict[str, Any] | None:
        ...

    @abstractmethod
    def find_by_task(self, task_id: str) -> DocumentRecord | None:
        ...

    def list_for_patient(self, patient_id: str) -> list[DocumentRecord]:
        """Optional; default empty. In-memory store overrides."""
        return []


class InMemoryDocumentStore(DocumentStore):
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: dict[str, DocumentRecord] = {}
        self._bytes: dict[str, bytes] = {}

    def save(self, record: DocumentRecord, content: bytes | None = None) -> DocumentRecord:
        with self._lock:
            if not record.document_id:
                record.document_id = str(uuid.uuid4())
            self._records[record.document_id] = record
            if content is not None:
                self._bytes[record.document_id] = content
                record.output_ref = f"memory://{record.document_id}"
            return record

    def retrieve(self, document_id: str) -> DocumentRecord | None:
        return self._records.get(document_id)

    def get_content(self, document_id: str) -> bytes | None:
        return self._bytes.get(document_id)

    def metadata(self, document_id: str) -> dict[str, Any] | None:
        rec = self._records.get(document_id)
        return rec.to_safe_dict() if rec else None

    def find_by_task(self, task_id: str) -> DocumentRecord | None:
        for rec in self._records.values():
            if rec.task_id == task_id:
                return rec
        return None

    def list_for_patient(self, patient_id: str) -> list[DocumentRecord]:
        return [r for r in self._records.values() if r.patient_id == patient_id]
