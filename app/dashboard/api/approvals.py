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

router = APIRouter(prefix="/api/approvals", tags=["approvals"])


@router.get("", response_model=list[TaskOut])
def list_approvals(
    user: Annotated[User, Depends(require_permission(Permission.APPROVAL_VIEW))],
    svc: Annotated[ApprovalService, Depends(approval_service)],
    correlation_id: Annotated[str, Depends(get_correlation_id)],
    status_filter: str | None = None,
    task_type: str | None = None,
) -> list[TaskOut]:
    return svc.list_tasks(
        actor=user.user_id,
        role=user.role,
        correlation_id=correlation_id,
        status=status_filter,
        task_type=task_type,
        pending_only=status_filter is None,
    )


@router.get("/{task_id}", response_model=TaskOut)
def get_approval(
    task_id: str,
    user: Annotated[User, Depends(require_permission(Permission.APPROVAL_VIEW))],
    svc: Annotated[ApprovalService, Depends(approval_service)],
) -> TaskOut:
    try:
        return svc.get_task(task_id)
    except ApprovalError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="task_not_found") from None


@router.post("/{task_id}/approve", response_model=TaskOut)
def approve_task(
    task_id: str,
    user: Annotated[User, Depends(require_permission(Permission.APPROVAL_APPROVE))],
    svc: Annotated[ApprovalService, Depends(approval_service)],
    correlation_id: Annotated[str, Depends(get_correlation_id)],
    body: ReviewActionRequest | None = None,
) -> TaskOut:
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
def reject_task(
    task_id: str,
    user: Annotated[User, Depends(require_permission(Permission.APPROVAL_REJECT))],
    svc: Annotated[ApprovalService, Depends(approval_service)],
    correlation_id: Annotated[str, Depends(get_correlation_id)],
    body: ReviewActionRequest | None = None,
) -> TaskOut:
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
