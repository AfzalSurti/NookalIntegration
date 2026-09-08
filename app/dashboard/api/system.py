from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from app.dashboard.auth import User
from app.dashboard.authorization import Permission
from app.dashboard.dependencies import (
    get_correlation_id,
    require_permission,
    system_service,
)
from app.dashboard.schemas import KillSwitchRequest, SystemStatusOut
from app.dashboard.services import SystemService

router = APIRouter(prefix="/api/system", tags=["system"])


@router.get("/status", response_model=SystemStatusOut)
def system_status(
    user: Annotated[User, Depends(require_permission(Permission.SYSTEM_VIEW))],
    svc: Annotated[SystemService, Depends(system_service)],
    correlation_id: Annotated[str, Depends(get_correlation_id)],
) -> SystemStatusOut:
    return svc.status(
        actor=user.user_id,
        role=user.role,
        correlation_id=correlation_id,
    )


@router.post("/kill-switch", response_model=SystemStatusOut)
def set_kill_switch(
    body: KillSwitchRequest,
    user: Annotated[User, Depends(require_permission(Permission.SYSTEM_KILL_SWITCH))],
    svc: Annotated[SystemService, Depends(system_service)],
    correlation_id: Annotated[str, Depends(get_correlation_id)],
) -> SystemStatusOut:
    return svc.set_kill_switch(
        actor=user.user_id,
        role=user.role,
        correlation_id=correlation_id,
        body=body,
    )
