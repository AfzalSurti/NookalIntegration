"""Validate documents before render — draft cannot become final without approval."""
from __future__ import annotations

from typing import Any

from app.letters.models import DocumentRecord, DocumentStatus
from app.letters.templates import TemplateError, TemplateSpec, TemplateStore
from app.shared.exceptions import AutomationError


class DocumentValidationError(AutomationError):
    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None) -> None:
        self.code = code
        self.details = details or {}
        super().__init__(message)


def validate_for_render(
    record: DocumentRecord,
    *,
    templates: TemplateStore | None = None,
    require_approved: bool = True,
) -> TemplateSpec:
    if not record.document_id or not record.patient_id or not record.task_id:
        raise DocumentValidationError(
            "missing_refs",
            "document_id, patient_id, and task_id are required",
        )
    if not record.template_id:
        raise DocumentValidationError("missing_template", "template_id is required")

    if require_approved and record.status not in {
        DocumentStatus.APPROVED,
        DocumentStatus.RENDERED,
        DocumentStatus.STORED,
        DocumentStatus.DELIVERED,
    }:
        raise DocumentValidationError(
            "not_approved",
            "document must be approved before final render",
            details={"status": record.status.value},
        )

    if record.status == DocumentStatus.DRAFT:
        raise DocumentValidationError(
            "draft_not_final",
            "DRAFT content cannot be rendered as final output",
        )
    if record.status == DocumentStatus.REJECTED:
        raise DocumentValidationError(
            "rejected",
            "rejected documents cannot be rendered",
        )

    body = record.approved_body
    if require_approved and not body:
        raise DocumentValidationError(
            "missing_approved_body",
            "approved_body is required for final render",
        )

    store = templates or TemplateStore()
    try:
        spec = store.get(record.template_id)
    except TemplateError as exc:
        raise DocumentValidationError("template_error", str(exc)) from exc

    if spec.document_type != record.document_type.value:
        raise DocumentValidationError(
            "type_mismatch",
            "document_type does not match template",
            details={
                "document_type": record.document_type.value,
                "template_type": spec.document_type,
            },
        )

    # Merge approved body into facts for templates that use {body}.
    values = dict(record.source_facts)
    if body is not None:
        values.setdefault("body", body)

    try:
        # Dry-run render to validate placeholders without accepting blanks.
        spec.render(values)
    except TemplateError as exc:
        raise DocumentValidationError("placeholder_error", str(exc)) from exc

    return spec
