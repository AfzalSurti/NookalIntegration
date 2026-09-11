"""Communication status API routes — Section 4.6."""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends

from app.dashboard.auth import User
from app.dashboard.authorization import Permission
from app.dashboard.dependencies import (
    get_correlation_id,
    require_permission,
    communication_service,
)
from app.dashboard.services.communication import CommunicationDashboardService

router = APIRouter(prefix="/api/communication", tags=["communication"])


@router.get("/status")
def communication_status(
    user: Annotated[User, Depends(require_permission(Permission.COMMUNICATION_VIEW))],
    svc: Annotated[CommunicationDashboardService, Depends(communication_service)],
    correlation_id: Annotated[str, Depends(get_correlation_id)],
) -> dict[str, Any]:
    """Overall communication configuration and capability status."""
    return svc.get_overview(
        actor=user.user_id,
        role=user.role,
        correlation_id=correlation_id,
    )


@router.get("/workflows")
def communication_workflows(
    user: Annotated[User, Depends(require_permission(Permission.COMMUNICATION_VIEW))],
    svc: Annotated[CommunicationDashboardService, Depends(communication_service)],
    correlation_id: Annotated[str, Depends(get_correlation_id)],
) -> list[dict[str, Any]]:
    """Per-workflow capability matrix."""
    return svc.get_workflows(
        actor=user.user_id,
        role=user.role,
        correlation_id=correlation_id,
    )


@router.get("/patient/{patient_id}/readiness")
def patient_communication_readiness(
    patient_id: str,
    user: Annotated[User, Depends(require_permission(Permission.COMMUNICATION_VIEW))],
    svc: Annotated[CommunicationDashboardService, Depends(communication_service)],
    correlation_id: Annotated[str, Depends(get_correlation_id)],
) -> dict[str, Any]:
    """Check whether a patient can receive SMS and/or email."""
    return svc.get_patient_readiness(
        patient_id,
        actor=user.user_id,
        role=user.role,
        correlation_id=correlation_id,
    )
