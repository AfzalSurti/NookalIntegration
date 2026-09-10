"""
Referral thank-you letter drafting — Task pending_review only.

PDF render / Nookal save / send happen later via approval handlers (not this phase).
"""
from __future__ import annotations

from typing import Any, ClassVar

from app.approval import TaskStatus, TaskType
from app.orchestration.base import BaseWorkflow
from app.orchestration.context import WorkflowContext
from app.orchestration.results import WorkflowResult
from app.shared.exceptions import NookalError

# Client-supplied letter skeleton; narrative filled by constrained LLM draft.
DEFAULT_LETTER_TEMPLATE = (
    "Dear {referrer_name},\n\n"
    "Thank you for referring your patient.\n\n"
    "{body}\n\n"
    "Kind regards,\n"
    "Back to Ease Physiotherapy\n"
)


class ReferralThankYouWorkflow(BaseWorkflow):
    name: ClassVar[str] = "referral_thank_you"

    def execute(
        self,
        ctx: WorkflowContext,
        *,
        referral_id: str,
        template: str = DEFAULT_LETTER_TEMPLATE,
        **_extra: Any,
    ) -> WorkflowResult:
        if ctx.llm is None:
            return WorkflowResult.failed(
                self.name,
                ctx.correlation_id,
                code="llm_required",
                message="llm service not configured",
            )

        ctx.audit_event(
            "referral_thank_you.trigger",
            target_type="patient_record",
            target_id=referral_id,
        )

        get_referral = getattr(ctx.nookal, "get_referral", None)
        if not callable(get_referral):
            return WorkflowResult.failed(
                self.name,
                ctx.correlation_id,
                code="referrals_unsupported",
                message="nookal client does not expose referrals",
            )

        try:
            referral = get_referral(referral_id)
            patient = ctx.nookal.get_patient(referral.patient_id)
            if hasattr(ctx.nookal, "list_referrers"):
                referrers = {r.referrer_id: r for r in ctx.nookal.list_referrers()}
                referrer = referrers.get(referral.referrer_id)
            else:
                referrer = None
        except NotImplementedError:
            referrer = None
        except NookalError as exc:
            ctx.audit_event(
                "referral_thank_you.lookup_failed",
                target_type="patient_record",
                target_id=referral_id,
                result="failure",
                metadata={"error_type": type(exc).__name__},
            )
            return WorkflowResult.failed(
                self.name,
                ctx.correlation_id,
                code="lookup_failed",
                message="failed to load referral context",
                details={"error_type": type(exc).__name__},
            )

        if referrer is None:
            return WorkflowResult.needs_human(
                self.name,
                ctx.correlation_id,
                code="referrer_missing",
                message="referrer not found for referral",
                details={"referral_id": referral_id, "referrer_id": referral.referrer_id},
            )

        facts = {
            "patient_id": patient.patient_id,
            "referrer_name": referrer.name,
            "referrer_id": referrer.referrer_id,
            "referral_id": referral.referral_id,
            "recorded_on": referral.recorded_on.isoformat(),
        }
        # Never pass clinical free text; notes_ref is an opaque id only.
        if referral.notes_ref:
            facts["notes_ref"] = referral.notes_ref

        ctx.audit_event(
            "referral_thank_you.context_loaded",
            target_type="patient_record",
            target_id=referral_id,
            metadata={"patient_id": patient.patient_id, "referrer_id": referrer.referrer_id},
        )

        required = ["referrer_name", "recorded_on"]
        try:
            draft = ctx.llm.draft(
                template,
                facts,
                letter_type="referral thank-you letter",
                required_fields=required,
            )
        except Exception as exc:
            ctx.audit_event(
                "referral_thank_you.draft_failed",
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

        ctx.audit_event(
            "referral_thank_you.draft_generated",
            target_type="document",
            target_id=referral_id,
            metadata={"missing_count": len(missing), "tagged_status": tagged},
        )

        task = ctx.approval.create_task(
            task_type=TaskType.LETTER,
            patient_id=patient.patient_id,
            created_by="llm",
            content_draft={
                "letter_type": "referral_thank_you",
                "referral_id": referral.referral_id,
                "referrer_id": referrer.referrer_id,
                "referrer_name": referrer.name,
                "recorded_on": referral.recorded_on.isoformat(),
                "status_tag": tagged,
                "missing_fields": missing,
                "draft_body": draft_text,
                "source_facts": {
                    "referrer_name": referrer.name,
                    "referrer_id": referrer.referrer_id,
                    "referral_id": referral.referral_id,
                    "recorded_on": referral.recorded_on.isoformat(),
                    "patient_label": f"patient:{patient.patient_id}",
                },
                "template_id": "referral_thank_you",
            },
            submit=True,
        )
        ctx.audit_event(
            "referral_thank_you.task_created",
            target_type="task",
            target_id=task.id,
            metadata={"status": task.status.value, "missing_count": len(missing)},
        )

        assert task.status == TaskStatus.PENDING_REVIEW
        return WorkflowResult.success(
            self.name,
            ctx.correlation_id,
            data={
                "task_id": task.id,
                "patient_id": patient.patient_id,
                "referral_id": referral.referral_id,
                "status": task.status.value,
                "missing_fields": missing,
                "status_tag": tagged,
            },
        )
