from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from app.dashboard.auth import User
from app.dashboard.authorization import Permission
from app.dashboard.dependencies import (
    get_correlation_id,
    patient_service,
    require_permission,
)
from app.dashboard.schemas import PatientDetail, PatientSearchResponse
from app.dashboard.services import PatientService
from app.shared.exceptions import NookalNotFound

router = APIRouter(prefix="/api/patients", tags=["patients"])


@router.get("/search", response_model=PatientSearchResponse)
def search_patients(
    user: Annotated[User, Depends(require_permission(Permission.PATIENT_SEARCH))],
    svc: Annotated[PatientService, Depends(patient_service)],
    correlation_id: Annotated[str, Depends(get_correlation_id)],
    suburb: str | None = None,
    age_min: int | None = None,
    age_max: int | None = None,
    appointment_from: date | None = None,
    appointment_to: date | None = None,
    referrer_id: str | None = None,
    page: int = 1,
    page_size: int = 25,
) -> PatientSearchResponse:
    return svc.search(
        actor=user.user_id,
        role=user.role,
        correlation_id=correlation_id,
        suburb=suburb,
        age_min=age_min,
        age_max=age_max,
        appointment_from=appointment_from,
        appointment_to=appointment_to,
        referrer_id=referrer_id,
        page=page,
        page_size=page_size,
    )


@router.get("/{patient_id}", response_model=PatientDetail)
def get_patient(
    patient_id: str,
    user: Annotated[User, Depends(require_permission(Permission.PATIENT_VIEW))],
    svc: Annotated[PatientService, Depends(patient_service)],
    correlation_id: Annotated[str, Depends(get_correlation_id)],
) -> PatientDetail:
    try:
        return svc.get_detail(
            patient_id,
            actor=user.user_id,
            role=user.role,
            correlation_id=correlation_id,
        )
    except NookalNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="patient_not_found") from None
