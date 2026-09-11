from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from app.approval import TaskType
from app.dashboard.auth import User
from app.dashboard.authorization import Permission
from app.dashboard.container import DashboardContainer
from app.dashboard.dependencies import (
    approval_service,
    get_container,
    get_correlation_id,
    require_permission,
)
from app.dashboard.schemas import DocumentDraftRequest, ReviewActionRequest, TaskOut
from app.dashboard.services import ApprovalService
from app.dashboard.services.approvals import task_to_out
from app.shared.exceptions import ApprovalError, NookalNotFound, StateTransitionError

router = APIRouter(prefix="/api/documents", tags=["documents"])


@router.post("/request", response_model=TaskOut)
def request_document_draft(
    req: DocumentDraftRequest,
    user: Annotated[User, Depends(require_permission(Permission.APPROVAL_VIEW))],
    container: Annotated[DashboardContainer, Depends(get_container)],
    correlation_id: Annotated[str, Depends(get_correlation_id)],
) -> TaskOut:
    """Request generation of a certificate or clinical letter draft for human review."""
    try:
        patient = container.nookal.get_patient(req.patient_id)
    except NookalNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"Patient {req.patient_id} not found")
    except Exception as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Patient lookup failed: {exc}")

    if patient is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"Patient {req.patient_id} not found")

    patient_label = patient.display_name or f"patient:{patient.patient_id}"

    if req.document_type == "certificate":
        task_type = TaskType.CERTIFICATE
        cert_type = req.certificate_type or "attendance"
        statement = (req.notes or "").strip() or f"Certificate of {cert_type} for patient {patient_label}."
        content_draft = {
            "template_id": "certificate",
            "certificate_type": cert_type,
            "status_tag": "DRAFT",
            "draft_body": statement,
            "patient_display_label": patient_label,
            "source_facts": {
                "patient_label": patient_label,
                "certificate_type": cert_type,
                "statement": statement,
            },
        }
    elif req.document_type == "referral_thank_you":
        task_type = TaskType.LETTER
        ref_name = getattr(patient, "referrer_name", None) or (patient.referrer_id and f"Dr. {patient.referrer_id}") or "Referring Doctor"
        statement = (req.notes or "").strip() or "Thank you for referring your patient to Back to Ease Physiotherapy."
        content_draft = {
            "template_id": "referral_thank_you",
            "letter_type": "referral_thank_you",
            "status_tag": "DRAFT",
            "referrer_name": ref_name,
            "referrer_id": patient.referrer_id,
            "draft_body": statement,
            "patient_display_label": patient_label,
            "source_facts": {
                "patient_label": patient_label,
                "referrer_name": ref_name,
                "statement": statement,
            },
        }
    else:
        # treatment_completion or progress_letter
        task_type = TaskType.LETTER
        default_stmt = "Treatment completed successfully." if req.document_type == "treatment_completion" else "Patient is making satisfactory rehabilitation progress."
        statement = (req.notes or "").strip() or default_stmt
        content_draft = {
            "template_id": req.document_type,
            "letter_type": req.document_type,
            "status_tag": "DRAFT",
            "draft_body": statement,
            "patient_display_label": patient_label,
            "source_facts": {
                "patient_label": patient_label,
                "statement": statement,
            },
        }

    task = container.approval.create_task(
        task_type=task_type,
        patient_id=patient.patient_id,
        content_draft=content_draft,
        created_by=user.user_id,
    )

    container.audit(
        actor=user.user_id,
        action="document.draft_requested",
        target_type="patient_record",
        target_id=patient.patient_id,
        result="success",
        metadata={
            "task_id": task.id,
            "document_type": req.document_type,
            "correlation_id": correlation_id,
            "role": user.role,
        },
    )

    return task_to_out(task)


@router.get("/review", response_model=list[TaskOut])
def list_document_reviews(
    user: Annotated[User, Depends(require_permission(Permission.APPROVAL_VIEW))],
    svc: Annotated[ApprovalService, Depends(approval_service)],
    correlation_id: Annotated[str, Depends(get_correlation_id)],
) -> list[TaskOut]:
    return svc.document_tasks(
        actor=user.user_id,
        role=user.role,
        correlation_id=correlation_id,
    )


@router.post("/{task_id}/approve", response_model=TaskOut)
def approve_document(
    task_id: str,
    user: Annotated[User, Depends(require_permission(Permission.APPROVAL_APPROVE))],
    svc: Annotated[ApprovalService, Depends(approval_service)],
    correlation_id: Annotated[str, Depends(get_correlation_id)],
    body: ReviewActionRequest | None = None,
) -> TaskOut:
    task = svc.get_task(task_id)
    if task.type not in {"letter", "certificate"}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="not_a_document_task")
    try:
        return svc.approve(
            task_id,
            reviewer_id=user.user_id,
            correlation_id=correlation_id,
            body=body,
        )
    except StateTransitionError:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="invalid_state") from None
    except ApprovalError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None


@router.post("/{task_id}/reject", response_model=TaskOut)
def reject_document(
    task_id: str,
    user: Annotated[User, Depends(require_permission(Permission.APPROVAL_REJECT))],
    svc: Annotated[ApprovalService, Depends(approval_service)],
    correlation_id: Annotated[str, Depends(get_correlation_id)],
    body: ReviewActionRequest | None = None,
) -> TaskOut:
    task = svc.get_task(task_id)
    if task.type not in {"letter", "certificate"}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="not_a_document_task")
    try:
        return svc.reject(
            task_id,
            reviewer_id=user.user_id,
            correlation_id=correlation_id,
            body=body,
        )
    except StateTransitionError:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="invalid_state") from None
    except ApprovalError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="task_not_found") from None
