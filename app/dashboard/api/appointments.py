from __future__ import annotations

from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status

from app.dashboard.auth import User
from app.dashboard.authorization import Permission
from app.dashboard.dependencies import (
    appointment_service,
    get_correlation_id,
    require_permission,
)
from app.dashboard.schemas import AppointmentActionRequest, AppointmentSummary
from app.dashboard.services import AppointmentService

router = APIRouter(prefix="/api/appointments", tags=["appointments"])


@router.get("", response_model=list[AppointmentSummary])
def list_appointments(
    user: Annotated[User, Depends(require_permission(Permission.APPOINTMENT_VIEW))],
    svc: Annotated[AppointmentService, Depends(appointment_service)],
    correlation_id: Annotated[str, Depends(get_correlation_id)],
    date_from: date | None = None,
    date_to: date | None = None,
    patient_id: str | None = None,
) -> list[AppointmentSummary]:
    return svc.list_upcoming(
        actor=user.user_id,
        role=user.role,
        correlation_id=correlation_id,
        date_from=date_from,
        date_to=date_to,
        patient_id=patient_id,
    )


@router.post("/actions")
def appointment_action(
    body: AppointmentActionRequest,
    user: Annotated[User, Depends(require_permission(Permission.APPOINTMENT_CHANGE))],
    svc: Annotated[AppointmentService, Depends(appointment_service)],
    correlation_id: Annotated[str, Depends(get_correlation_id)],
) -> dict[str, Any]:
    try:
        result = svc.run_action(
            actor=user.user_id,
            role=user.role,
            correlation_id=correlation_id,
            body=body,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    return {
        "workflow": result.workflow,
        "status": result.status.value,
        "correlation_id": result.correlation_id,
        "data": result.data,
        "error": result.error.to_dict() if result.error else None,
    }
