"""
Certificate request via messaging — creates pending_review Task only.

PDF generation, dashboard UI, and Nookal document save-back are later phases.
Approval handlers are not registered here; no side effects before human approval.
"""
from __future__ import annotations

from typing import Any, ClassVar

from app.approval import TaskStatus, TaskType
from app.orchestration.base import BaseWorkflow
from app.orchestration.context import WorkflowContext
from app.orchestration.identity import resolve_patient_by_phone
from app.orchestration.results import WorkflowResult


class CertificateRequestWorkflow(BaseWorkflow):
    name: ClassVar[str] = "certificate_request"

    def execute(
        self,
        ctx: WorkflowContext,
        *,
        phone: str,
        message: str,
        **_extra: Any,
    ) -> WorkflowResult:
        if ctx.llm is None:
            return WorkflowResult.failed(
                self.name,
                ctx.correlation_id,
                code="llm_required",
                message="llm service not configured",
            )

        try:
            intent = ctx.llm.parse_intent(message)
        except Exception as exc:
            ctx.audit_event(
                "certificate_request.llm_failed",
                result="failure",
                metadata={"error_type": type(exc).__name__},
            )
            return WorkflowResult.failed(
                self.name,
                ctx.correlation_id,
                code="llm_failure",
                message="intent parsing failed",
                details={"error_type": type(exc).__name__},
            )

        if getattr(intent, "confidence", "low") != "high" or getattr(intent, "intent", "") != "request_certificate":
            return WorkflowResult.needs_human(
                self.name,
                ctx.correlation_id,
                code="intent_not_actionable",
                message="intent ambiguous or not certificate request",
            )

        fields = getattr(intent, "extracted_fields", {}) or {}
        cert_type = fields.get("certificate_type") or fields.get("type")
        if not cert_type:
            return WorkflowResult.needs_human(
                self.name,
                ctx.correlation_id,
                code="missing_certificate_type",
                message="certificate type not explicitly provided",
            )

        resolution = resolve_patient_by_phone(ctx.nookal, phone)
        ctx.audit_event(
            "certificate_request.identity",
            target_type="patient_record",
            target_id="phone_lookup",
            result="success" if resolution.status == "matched" else "failure",
            metadata={"status": resolution.status, "match_count": resolution.match_count},
        )
        if resolution.status == "not_found":
            return WorkflowResult.needs_human(
                self.name,
                ctx.correlation_id,
                code="patient_not_found",
                message="no patient matched phone",
            )
        if resolution.status == "ambiguous":
            return WorkflowResult.needs_human(
                self.name,
                ctx.correlation_id,
                code="patient_ambiguous",
                message="multiple patients matched phone",
                details={"match_count": resolution.match_count},
            )

        patient = resolution.patient
        assert patient is not None

        # Draft content for staff review — no clinical invention; type only.
        task = ctx.approval.create_task(
            task_type=TaskType.CERTIFICATE,
            patient_id=patient.patient_id,
            created_by="llm",
            content_draft={
                "certificate_type": str(cert_type),
                "status_tag": "DRAFT",
                "source": "whatsapp_request",
                "template_id": "certificate",
                "source_facts": {
                    "certificate_type": str(cert_type),
                    "patient_label": f"patient:{patient.patient_id}",
                },
            },
            submit=True,
        )
        ctx.audit_event(
            "certificate_request.task_created",
            target_type="task",
            target_id=task.id,
            metadata={"task_type": task.type.value, "status": task.status.value},
        )

        assert task.status == TaskStatus.PENDING_REVIEW
        return WorkflowResult.success(
            self.name,
            ctx.correlation_id,
            data={
                "task_id": task.id,
                "patient_id": patient.patient_id,
                "status": task.status.value,
                "certificate_type": str(cert_type),
            },
        )
