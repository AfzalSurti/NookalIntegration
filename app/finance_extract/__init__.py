"""Constrained, draft-only extraction of receipt and invoice facts."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Protocol

from app.approval import ApprovalQueue, Task, TaskType
from app.llm_service import ExpenseExtractionResult, LLMService
from app.shared import audit as audit_mod


ExpenseStatus = Literal["extracted", "pending_review", "failed"]


@dataclass(frozen=True)
class ExpenseRecord:
    document_id: str
    source_path: str
    date: str | None
    supplier: str | None
    amount: str | None
    gst: str | None
    description: str | None
    category: str | None
    payment_reference: str | None
    missing_fields: tuple[str, ...]
    extraction_confidence: float
    status: ExpenseStatus
    review_task_id: str | None
    created_at: str


class OcrAdapter(Protocol):
    def extract_text(self, *, path: Path, mime_type: str) -> str:
        ...


class ExpenseExtractor(Protocol):
    def extract_expense(self, document_text: str) -> ExpenseExtractionResult:
        ...


AuditFn = Callable[..., Any]


class FinanceExtractionError(RuntimeError):
    def __init__(self, message: str, record: ExpenseRecord) -> None:
        super().__init__(message)
        self.record = record


class FinanceExtract:
    def __init__(
        self,
        llm: ExpenseExtractor | LLMService,
        ocr: OcrAdapter,
        approval: ApprovalQueue,
        *,
        confidence_threshold: float = 0.8,
        audit: AuditFn | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not 0.0 <= confidence_threshold <= 1.0:
            raise ValueError("confidence_threshold must be between 0 and 1")
        self._llm = llm
        self._ocr = ocr
        self._approval = approval
        self._threshold = confidence_threshold
        self._audit = audit or audit_mod.log_event
        self._now = now or (lambda: datetime.now(timezone.utc))

    def extract(self, *, document_id: str, path: Path, mime_type: str) -> ExpenseRecord:
        try:
            result = self._llm.extract_expense(
                self._ocr.extract_text(path=path, mime_type=mime_type)
            )
            fields = result.fields
            status: ExpenseStatus = (
                "pending_review" if result.confidence < self._threshold else "extracted"
            )
            task_id: str | None = None
            if status == "pending_review":
                task = self._approval.create_task(
                    task_type=TaskType.EXPENSE_EXTRACTION,
                    patient_id="unknown",
                    created_by="llm",
                    content_draft={"document_id": document_id, "fields": fields},
                )
                task_id = task.id
            record = _record(document_id, path, fields, result.confidence, status, task_id, self._now())
            self._audit(
                "finance_extract", "extract_expense", "document", document_id, "success",
                metadata={"status": status, "result": "extracted"},
            )
            return record
        except Exception as exc:
            failed = ExpenseRecord(
                document_id=document_id, source_path=str(path), date=None, supplier=None,
                amount=None, gst=None, description=None, category=None,
                payment_reference=None, missing_fields=(), extraction_confidence=0.0,
                status="failed", review_task_id=None, created_at=self._now().isoformat(),
            )
            self._audit(
                "finance_extract", "extract_expense", "document", document_id, "failure",
                metadata={"error_type": type(exc).__name__},
            )
            raise FinanceExtractionError("expense extraction failed", failed) from exc


def _record(
    document_id: str,
    path: Path,
    fields: dict[str, Any],
    confidence: float,
    status: ExpenseStatus,
    task_id: str | None,
    created_at: datetime,
) -> ExpenseRecord:
    missing = tuple(fields.get("missing_fields") or ())
    return ExpenseRecord(
        document_id=document_id,
        source_path=str(path),
        date=fields.get("date"),
        supplier=fields.get("supplier"),
        amount=fields.get("amount"),
        gst=fields.get("gst"),
        description=fields.get("description"),
        category=fields.get("category"),
        payment_reference=fields.get("payment_reference"),
        missing_fields=missing,
        extraction_confidence=confidence,
        status=status,
        review_task_id=task_id,
        created_at=created_at.isoformat(),
    )


__all__ = ["ExpenseRecord", "FinanceExtract", "FinanceExtractionError", "OcrAdapter"]