"""
Treatment completion / progress letter drafting — same approval → PDF pipeline.
"""
from __future__ import annotations

from typing import Any, ClassVar

from app.approval import TaskStatus, TaskType
from app.orchestration.base import BaseWorkflow
from app.orchestration.context import WorkflowContext
from app.orchestration.results import WorkflowResult
from app.shared.exceptions import NookalError

DEFAULT_COMPLETION_TEMPLATE = (
    "Treatment completion note for patient reference {patient_label}.\n\n"
    "{body}\n"
)


class TreatmentCompletionLetterWorkflow(BaseWorkflow):
    name: ClassVar[str] = "treatment_completion_letter"

    def execute(
        self,
        ctx: WorkflowContext,
        *,
        patient_id: str,
        facts: dict[str, Any] | None = None,
        letter_type: str = "treatment_completion",
        template: str = DEFAULT_COMPLETION_TEMPLATE,
        **_extra: Any,
    ) -> WorkflowResult:
        """
        facts: verified administrative fields only (no invented clinical content).
        letter_type: treatment_completion | progress_letter
        """
        if ctx.llm is None:
            return WorkflowResult.failed(
                self.name,
                ctx.correlation_id,
                code="llm_required",
                message="llm service not configured",
            )
        if letter_type not in {"treatment_completion", "progress_letter"}:
            return WorkflowResult.failed(
                self.name,
                ctx.correlation_id,
                code="invalid_letter_type",
                message="unsupported letter type",
            )

        try:
            patient = ctx.nookal.get_patient(patient_id)
        except NookalError as exc:
            return WorkflowResult.failed(
                self.name,
                ctx.correlation_id,
                code="patient_not_found",
                message="patient lookup failed",
                details={"error_type": type(exc).__name__},
            )

        verified = dict(facts or {})
        verified.setdefault("patient_label", f"patient:{patient.patient_id}")
        # Strip any accidentally supplied clinical keys.
        for banned in ("diagnosis", "clinical_notes", "symptoms"):
            verified.pop(banned, None)

        required = ["patient_label"]
        try:
            draft = ctx.llm.draft(
                template,
                verified,
                letter_type=letter_type.replace("_", " "),
                required_fields=required,
            )
        except Exception as exc:
            ctx.audit_event(
                "treatment_completion.draft_failed",
                result="failure",
                metadata={"error_type": type(exc).__name__},
            )
            return WorkflowResult.failed(
                self.name,
                ctx.correlation_id,
                code="llm_failure",
                message="draft generation failed",
                details={"error_type": type(exc).__name__},
            )

        missing = list(getattr(draft, "missing_fields", []) or [])
        draft_text = getattr(draft, "text", "")
        tagged = getattr(draft, "tagged_status", "DRAFT")
        template_id = (
            "treatment_completion"
            if letter_type == "treatment_completion"
            else "progress_letter"
        )

        task = ctx.approval.create_task(
            task_type=TaskType.LETTER,
            patient_id=patient.patient_id,
            created_by="llm",
            content_draft={
                "letter_type": letter_type,
                "status_tag": tagged,
                "missing_fields": missing,
                "draft_body": draft_text,
                "template_id": template_id,
                "completion_date": verified.get("completion_date"),
                "source_facts": {
                    "patient_label": verified["patient_label"],
                    **{
                        k: verified[k]
                        for k in ("completion_date", "referrer_name")
                        if k in verified
                    },
                },
            },
            submit=True,
        )
        ctx.audit_event(
            "treatment_completion.task_created",
            target_type="task",
            target_id=task.id,
            metadata={"status": task.status.value, "letter_type": letter_type},
        )
        assert task.status == TaskStatus.PENDING_REVIEW
        return WorkflowResult.success(
            self.name,
            ctx.correlation_id,
            data={
                "task_id": task.id,
                "patient_id": patient.patient_id,
                "status": task.status.value,
                "letter_type": letter_type,
                "missing_fields": missing,
            },
        )
