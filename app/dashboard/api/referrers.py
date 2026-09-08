from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status

from app.dashboard.auth import User
from app.dashboard.authorization import Permission
from app.dashboard.dependencies import (
    get_correlation_id,
    referrer_service,
    require_permission,
)
from app.dashboard.schemas import ReferrerConflictOut, ReferrerResolveRequest
from app.dashboard.services import ReferrerConflictService
from app.shared.exceptions import KillSwitchActive

router = APIRouter(prefix="/api/referrers", tags=["referrers"])


@router.get("/conflicts", response_model=list[ReferrerConflictOut])
def list_conflicts(
    user: Annotated[User, Depends(require_permission(Permission.REFERRER_VIEW))],
    svc: Annotated[ReferrerConflictService, Depends(referrer_service)],
    correlation_id: Annotated[str, Depends(get_correlation_id)],
) -> list[ReferrerConflictOut]:
    return svc.list_conflicts(
        actor=user.user_id,
        role=user.role,
        correlation_id=correlation_id,
    )


@router.post("/conflicts/{conflict_id}/resolve")
def resolve_conflict(
    conflict_id: str,
    body: ReferrerResolveRequest,
    user: Annotated[User, Depends(require_permission(Permission.REFERRER_RESOLVE))],
    svc: Annotated[ReferrerConflictService, Depends(referrer_service)],
    correlation_id: Annotated[str, Depends(get_correlation_id)],
) -> dict[str, Any]:
    try:
        return svc.resolve(
            conflict_id,
            actor=user.user_id,
            role=user.role,
            correlation_id=correlation_id,
            body=body,
        )
    except KeyError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="conflict_not_found") from None
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    except KillSwitchActive:
        raise HTTPException(status.HTTP_423_LOCKED, detail="kill_switch_active") from None
