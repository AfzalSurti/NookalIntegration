"""Human-authorised filing of preserved intake documents."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from app.doc_intake import DocumentRecord
from app.nookal_client import NookalClient
from app.shared import audit as audit_mod
from app.shared.exceptions import KillSwitchActive
from app.shared.kill_switch import assert_allows


class DriveAdapter(Protocol):
    """Approved Drive destination; production authentication is intentionally deferred."""

    def save_document(
        self, *, filename: str, content: bytes, content_type: str, folder: str | None = None
    ) -> str:
        """Return an opaque Drive reference after saving an unchanged copy."""
        ...


class UnavailableDriveAdapter:
    """Explicit placeholder until client-authorised Google Drive access is configured."""

    def save_document(
        self, *, filename: str, content: bytes, content_type: str, folder: str | None = None
    ) -> str:
        raise NotImplementedError(
            "TODO: implement Google Drive upload only after client-authorised access and official API design"
        )


AuditFn = Callable[..., Any]


class DocumentFilingError(RuntimeError):
    """Raised when filing cannot complete; the returned record is marked failed."""

    def __init__(self, message: str, record: DocumentRecord) -> None:
        super().__init__(message)
        self.record = record


class DocumentFiler:
    """File only a resolved patient document and return its explicit filing state."""

    def __init__(
        self,
        nookal: NookalClient,
        *,
        drive: DriveAdapter | None = None,
        audit: AuditFn | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._nookal = nookal
        self._drive = drive
        self._audit = audit or audit_mod.log_event
        self._now = now or (lambda: datetime.now(timezone.utc))

    def file(
        self,
        document: DocumentRecord,
        *,
        filed_by: str,
        to_nookal: bool = True,
        to_drive: bool = False,
    ) -> DocumentRecord:
        """File unchanged bytes to selected targets; unresolved matches are rejected."""
        if document.patient_match.status != "matched" or not document.patient_match.patient_id:
            self._audit("doc_filing", "file_document", "document", document.document_id, "failure", metadata={"reason": "unresolved_patient"})
            raise ValueError("document must have a human-confirmed patient match")
        if not to_nookal and not to_drive:
            self._audit("doc_filing", "file_document", "document", document.document_id, "failure", metadata={"reason": "no_target"})
            raise ValueError("at least one filing target is required")
        if to_drive and self._drive is None:
            self._audit("doc_filing", "file_document", "document", document.document_id, "failure", metadata={"reason": "drive_unavailable"})
            raise ValueError("drive adapter is required when to_drive=True")

        content = Path(document.preserved_path).read_bytes()
        completed: list[str] = list(document.filed_to)
        filed_at = self._now().isoformat()
        current = replace(document, filed_by=filed_by, filed_at=filed_at)

        try:
            if to_nookal:
                self._check_kill_switch("doc_filing.nookal")
                self._nookal.save_document(
                    document.patient_match.patient_id,
                    title=document.original_filename,
                    content=content,
                    content_type=document.mime_type,
                )
                completed.append("nookal")
                self._audit(
                    "doc_filing", "file_document", "document", document.document_id, "success",
                    metadata={"target": "nookal"},
                )
            if to_drive:
                self._check_kill_switch("doc_filing.drive")
                assert self._drive is not None
                self._drive.save_document(
                    filename=document.original_filename,
                    content=content,
                    content_type=document.mime_type,
                )
                completed.append("drive")
                self._audit(
                    "doc_filing", "file_document", "document", document.document_id, "success",
                    metadata={"target": "drive"},
                )
        except Exception as exc:
            failed = replace(current, filed_to=tuple(completed), filing_status="failed")
            self._audit(
                "doc_filing", "file_document", "document", document.document_id, "blocked"
                if isinstance(exc, KillSwitchActive) else "failure",
                metadata={"completed_target_count": len(completed)},
            )
            if isinstance(exc, KillSwitchActive):
                raise
            raise DocumentFilingError("document filing failed", failed) from exc

        return replace(current, filed_to=tuple(completed), filing_status="filed")

    def cleanup_preserved(self, document: DocumentRecord, *, actor: str) -> None:
        """Remove a caller-designated temporary preserved copy after filing or failure handling."""
        path = Path(document.preserved_path)
        if path.exists():
            path.unlink()
        self._audit(
            actor, "cleanup_preserved_document", "document", document.document_id, "success",
            metadata={},
        )

    def _check_kill_switch(self, operation: str) -> None:
        assert_allows(operation)


def fake_drive_adapter() -> "FakeDriveAdapter":
    """Create the test-only Drive implementation without any external access."""
    return FakeDriveAdapter()


class FakeDriveAdapter:
    """In-memory Drive fake for synthetic tests."""

    def __init__(self) -> None:
        self.documents: list[tuple[str, bytes, str]] = []
        self.fail = False

    def save_document(
        self, *, filename: str, content: bytes, content_type: str, folder: str | None = None
    ) -> str:
        if self.fail:
            raise OSError("synthetic Drive failure")
        self.documents.append((filename, content, content_type))
        return f"fake-drive-{len(self.documents)}"


__all__ = [
    "DocumentFiler",
    "DocumentFilingError",
    "DriveAdapter",
    "FakeDriveAdapter",
    "UnavailableDriveAdapter",
    "fake_drive_adapter",
]