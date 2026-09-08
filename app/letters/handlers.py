"""
ApprovalQueue handlers for letter/certificate tasks.

approve() is the only entry that runs these side effects.
"""
from __future__ import annotations

import uuid
from typing import Any, Callable

from app.approval import ApprovalQueue, Task, TaskType
from app.letters.delivery import DocumentDelivery, InMemoryDocumentDelivery
from app.letters.models import DocumentRecord, DocumentStatus, DocumentType
from app.letters.renderer import DocumentRenderer, SimplePdfRenderer, content_hash, render_document_text
from app.letters.store import DocumentStore, InMemoryDocumentStore
from app.letters.templates import TemplateStore
from app.letters.validators import DocumentValidationError, validate_for_render
from app.shared.clock import Clock, SystemClock
from app.shared.exceptions import KillSwitchActive
from app.shared.kill_switch import assert_allows


AuditFn = Callable[..., Any]

_TASK_TO_DOC_TYPE = {
    TaskType.CERTIFICATE: DocumentType.CERTIFICATE,
    TaskType.LETTER: DocumentType.REFERRAL_THANK_YOU,  # overridden by content_draft.letter_type
}


def _doc_type_from_task(task: Task) -> DocumentType:
    letter_type = (task.content_draft or {}).get("letter_type")
    if letter_type == "treatment_completion":
        return DocumentType.TREATMENT_COMPLETION
    if letter_type == "progress_letter":
        return DocumentType.PROGRESS_LETTER
    if letter_type == "referral_thank_you":
        return DocumentType.REFERRAL_THANK_YOU
    if task.type == TaskType.CERTIFICATE:
        return DocumentType.CERTIFICATE
    return DocumentType.REFERRAL_THANK_YOU


def _template_id_for(doc_type: DocumentType, draft: dict[str, Any]) -> str:
    if draft.get("template_id"):
        return str(draft["template_id"])
    return {
        DocumentType.CERTIFICATE: "certificate",
        DocumentType.REFERRAL_THANK_YOU: "referral_thank_you",
        DocumentType.TREATMENT_COMPLETION: "treatment_completion",
        DocumentType.PROGRESS_LETTER: "progress_letter",
    }[doc_type]


class DocumentApprovalHandler:
    """
    Shared handler for certificate + letter task types.

    Idempotent on task_id: second approve path that somehow re-enters will
    short-circuit if a STORED/DELIVERED document already exists for the task.
    (ApprovalQueue itself prevents re-approve after SENT.)
    """

    def __init__(
        self,
        *,
        store: DocumentStore | None = None,
        delivery: DocumentDelivery | None = None,
        renderer: DocumentRenderer | None = None,
        templates: TemplateStore | None = None,
        clock: Clock | None = None,
        audit: AuditFn | None = None,
        deliver: bool = True,
        correlation_id: str | None = None,
    ) -> None:
        self.store = store or InMemoryDocumentStore()
        self.delivery = delivery or InMemoryDocumentDelivery()
        self.renderer = renderer or SimplePdfRenderer()
        self.templates = templates or TemplateStore()
        self.clock = clock or SystemClock()
        self.audit = audit
        self.deliver_enabled = deliver
        self.correlation_id = correlation_id

    def __call__(self, task: Task) -> None:
        self.handle(task)

    def handle(self, task: Task) -> DocumentRecord:
        existing = self.store.find_by_task(task.id)
        if existing and existing.status in {
            DocumentStatus.STORED,
            DocumentStatus.DELIVERED,
            DocumentStatus.RENDERED,
        }:
            self._audit(
                "document.duplicate_skipped",
                target_id=existing.document_id,
                result="success",
                metadata={"task_id": task.id, "reason": "already_processed"},
            )
            return existing

        draft = task.content_draft or {}
        if draft.get("status_tag") == "DRAFT" and not task.reviewed_by:
            # Should not happen if approve() set reviewed_by — belt and braces.
            raise DocumentValidationError(
                "draft_not_final",
                "unreviewed DRAFT cannot become final",
            )

        doc_type = _doc_type_from_task(task)
        template_id = _template_id_for(doc_type, draft)
        now = self.clock.now().isoformat()

        # Approved body: reviewer override or draft body.
        approved_body = draft.get("approved_body") or draft.get("draft_body") or draft.get("body")
        if doc_type == DocumentType.CERTIFICATE:
            # Certificate uses template fields; body may be a short statement.
            approved_body = approved_body or (
                f"Certificate of {draft.get('certificate_type', 'attendance')}."
            )

        source_facts = {
            k: v
            for k, v in (draft.get("source_facts") or {}).items()
            if k
            not in {
                "draft_body",
                "body",
                "clinical_notes",
                "diagnosis",
            }
        }
        # Safe administrative fields from draft.
        for key in (
            "certificate_type",
            "referrer_name",
            "referrer_id",
            "referral_id",
            "recorded_on",
            "patient_display_label",
            "completion_date",
            "treatment_summary_ref",
        ):
            if key in draft and key not in source_facts:
                source_facts[key] = draft[key]

        # Template requires some display fields — use opaque labels, not clinical text.
        source_facts.setdefault(
            "patient_label",
            source_facts.get("patient_display_label") or f"patient:{task.patient_id}",
        )
        if doc_type == DocumentType.CERTIFICATE:
            source_facts.setdefault(
                "certificate_type", draft.get("certificate_type") or "attendance"
            )
            source_facts.setdefault("statement", approved_body or "")

        record = DocumentRecord(
            document_id=str(uuid.uuid4()),
            document_type=doc_type,
            patient_id=task.patient_id,
            task_id=task.id,
            template_id=template_id,
            status=DocumentStatus.APPROVED,
            created_at=now,
            source_facts=source_facts,
            draft_body=draft.get("draft_body"),
            approved_body=approved_body,
            approved_at=now,
            approved_by=task.reviewed_by,
            metadata={"letter_type": draft.get("letter_type"), "missing_fields": draft.get("missing_fields") or []},
        )

        try:
            assert_allows("document.render")
        except KillSwitchActive:
            self._audit(
                "document.blocked",
                target_id=task.id,
                result="blocked",
                metadata={"reason": "kill_switch"},
            )
            raise

        spec = validate_for_render(record, templates=self.templates, require_approved=True)
        text = render_document_text(spec, record)
        pdf = self.renderer.render(text=text, metadata={"document_id": record.document_id})
        record.rendered_at = self.clock.now().isoformat()
        record.content_sha256 = content_hash(pdf)
        record.status = DocumentStatus.RENDERED

        self.store.save(record, content=pdf)
        record.status = DocumentStatus.STORED
        self.store.save(record, content=pdf)

        self._audit(
            "document.rendered",
            target_id=record.document_id,
            result="success",
            metadata={
                "task_id": task.id,
                "template_id": template_id,
                "sha256_prefix": (record.content_sha256 or "")[:12],
                "correlation_id": self.correlation_id,
            },
        )

        if self.deliver_enabled:
            try:
                assert_allows("document.deliver")
                self.delivery.deliver(
                    record,
                    destination=f"patient:{task.patient_id}",
                    channel="document",
                    metadata={"task_id": task.id},
                )
                record.status = DocumentStatus.DELIVERED
                self.store.save(record, content=pdf)
                self._audit(
                    "document.delivered",
                    target_id=record.document_id,
                    result="success",
                    metadata={"task_id": task.id},
                )
            except KillSwitchActive:
                self._audit(
                    "document.delivery_blocked",
                    target_id=record.document_id,
                    result="blocked",
                    metadata={"task_id": task.id},
                )
                raise

        return record

    def _audit(
        self,
        action: str,
        *,
        target_id: str,
        result: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if not self.audit:
            return
        meta = dict(metadata or {})
        # Never include bodies.
        for bad in ("body", "draft_body", "approved_body", "content", "text"):
            meta.pop(bad, None)
        self.audit(
            actor="document_handler",
            action=action,
            target_type="document",
            target_id=target_id,
            result=result,
            metadata=meta,
        )


def register_document_handlers(
    queue: ApprovalQueue,
    handler: DocumentApprovalHandler | None = None,
) -> DocumentApprovalHandler:
    h = handler or DocumentApprovalHandler()
    queue.register_handler(TaskType.CERTIFICATE, h)
    queue.register_handler(TaskType.LETTER, h)
    return h
