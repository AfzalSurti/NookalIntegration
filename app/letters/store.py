"""Document storage abstraction — Phase D uses in-memory fake."""
from __future__ import annotations

import json
import threading
import uuid
from abc import ABC, abstractmethod
from pathlib import Path
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


class FileSystemDocumentStore(DocumentStore):
    """Production filesystem-backed document store."""

    def __init__(self, directory: Path | str) -> None:
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _meta_path(self, document_id: str) -> Path:
        return self._dir / f"{document_id}.meta.json"

    def _content_path(self, document_id: str) -> Path:
        return self._dir / f"{document_id}.bin"

    def save(self, record: DocumentRecord, content: bytes | None = None) -> DocumentRecord:
        with self._lock:
            if not record.document_id:
                record.document_id = str(uuid.uuid4())
            if content is not None:
                c_path = self._content_path(record.document_id)
                c_path.write_bytes(content)
                record.output_ref = str(c_path)

            data = {
                "document_id": record.document_id,
                "document_type": record.document_type.value if hasattr(record.document_type, "value") else str(record.document_type),
                "patient_id": record.patient_id,
                "task_id": record.task_id,
                "template_id": record.template_id,
                "status": record.status.value if hasattr(record.status, "value") else str(record.status),
                "created_at": record.created_at,
                "source_facts": record.source_facts,
                "draft_body": record.draft_body,
                "approved_body": record.approved_body,
                "approved_at": record.approved_at,
                "approved_by": record.approved_by,
                "rendered_at": record.rendered_at,
                "output_ref": record.output_ref,
                "content_sha256": record.content_sha256,
                "metadata": record.metadata,
            }
            self._meta_path(record.document_id).write_text(
                json.dumps(data, indent=2), encoding="utf-8"
            )
            return record

    def retrieve(self, document_id: str) -> DocumentRecord | None:
        from app.letters.models import DocumentStatus, DocumentType

        path = self._meta_path(document_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return DocumentRecord(
                document_id=data["document_id"],
                document_type=DocumentType(data["document_type"]),
                patient_id=data["patient_id"],
                task_id=data["task_id"],
                template_id=data["template_id"],
                status=DocumentStatus(data["status"]),
                created_at=data["created_at"],
                source_facts=data.get("source_facts", {}),
                draft_body=data.get("draft_body"),
                approved_body=data.get("approved_body"),
                approved_at=data.get("approved_at"),
                approved_by=data.get("approved_by"),
                rendered_at=data.get("rendered_at"),
                output_ref=data.get("output_ref"),
                content_sha256=data.get("content_sha256"),
                metadata=data.get("metadata", {}),
            )
        except Exception:
            return None

    def get_content(self, document_id: str) -> bytes | None:
        path = self._content_path(document_id)
        if path.exists():
            return path.read_bytes()
        return None

    def metadata(self, document_id: str) -> dict[str, Any] | None:
        rec = self.retrieve(document_id)
        return rec.to_safe_dict() if rec else None

    def find_by_task(self, task_id: str) -> DocumentRecord | None:
        with self._lock:
            for path in self._dir.glob("*.meta.json"):
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    if data.get("task_id") == task_id:
                        return self.retrieve(data["document_id"])
                except Exception:
                    continue
        return None

    def list_for_patient(self, patient_id: str) -> list[DocumentRecord]:
        results: list[DocumentRecord] = []
        with self._lock:
            for path in self._dir.glob("*.meta.json"):
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    if data.get("patient_id") == patient_id:
                        rec = self.retrieve(data["document_id"])
                        if rec:
                            results.append(rec)
                except Exception:
                    continue
        return results

