from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from app.dashboard.auth import User
from app.dashboard.authorization import Permission
from app.dashboard.dependencies import (
    approval_service,
    get_correlation_id,
    require_permission,
)
from app.dashboard.schemas import ReviewActionRequest, TaskOut
from app.dashboard.services import ApprovalService
from app.shared.exceptions import ApprovalError, StateTransitionError

router = APIRouter(prefix="/api/documents", tags=["documents"])


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
