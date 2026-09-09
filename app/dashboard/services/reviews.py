"""Dashboard review workflows for documents, expenses, and case flags."""
from __future__ import annotations

from dataclasses import replace
from typing import Any, Callable

from app.doc_intake import DocumentRecord, PatientMatch
from app.finance_extract import ExpenseRecord


class ReviewService:
    def __init__(self, *, container: Any, audit: Callable[..., Any]) -> None:
        self._container = container
        self._audit = audit
        self.documents: dict[str, DocumentRecord] = {}
        self.expenses: dict[str, ExpenseRecord] = {}

    def upload(self, *, uploaded: Any, source_channel: str, actor: str, **hints: Any) -> DocumentRecord:
        record = self._require("document_intake").intake(uploaded, source_channel, **hints)
        self.documents[record.document_id] = record
        self._audit(actor=actor, action="dashboard.document_upload", target_type="document", target_id=record.document_id, result="success", metadata={"match_status": record.patient_match.status})
        return record

    def resolve_document(self, document_id: str, *, patient_id: str, actor: str) -> DocumentRecord:
        record = self.documents[document_id]
        if patient_id not in {candidate.patient_id for candidate in record.patient_match.candidates}:
            raise ValueError("patient_id is not a listed candidate")
        resolved = replace(record, patient_match=PatientMatch("matched", patient_id, 1.0))
        filed = self._require("document_filer").file(resolved, filed_by=actor)
        self.documents[document_id] = filed
        self._audit(actor=actor, action="dashboard.document_resolve", target_type="document", target_id=document_id, result="success", metadata={"target": "nookal"})
        return filed

    def list_expenses(self) -> list[ExpenseRecord]:
        return [expense for expense in self.expenses.values() if expense.status == "pending_review"]

    def confirm_expense(self, document_id: str, *, fields: dict[str, Any], category: str, actor: str) -> Any:
        expense = self.expenses[document_id]
        confirmed = replace(expense, **{key: value for key, value in fields.items() if key in {
            "date", "supplier", "amount", "gst", "description", "payment_reference"
        }}, category=category, status="extracted", missing_fields=tuple())
        categorized = self._require("finance_categorizer").categorize(confirmed, category=category, categorized_by=actor)
        self._require("finance_register").append(categorized)
        self.expenses[document_id] = categorized.expense
        self._audit(actor=actor, action="dashboard.expense_confirm", target_type="document", target_id=document_id, result="success", metadata={"category": category})
        return categorized

    def _require(self, name: str) -> Any:
        value = getattr(self._container, name, None)
        if value is None:
            raise RuntimeError(f"dashboard dependency unavailable: {name}")
        return value