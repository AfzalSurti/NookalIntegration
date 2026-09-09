from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from app.dashboard.auth import User
from app.dashboard.authorization import Permission
from app.dashboard.dependencies import require_permission, get_container
from app.dashboard.container import DashboardContainer

router = APIRouter(prefix="/api/cases", tags=["cases"])


@router.get("/flagged")
def flagged_cases(
    user: Annotated[User, Depends(require_permission(Permission.CASE_VIEW))],
    container: Annotated[DashboardContainer, Depends(get_container)],
) -> list[object]:
    tracker = getattr(container, "case_tracking", None)
    if tracker is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="case_tracking_unavailable")
    return tracker.flagged_cases()


@router.post("/{case_id}/acknowledge")
def acknowledge_case(
    case_id: str,
    user: Annotated[User, Depends(require_permission(Permission.CASE_ACKNOWLEDGE))],
    container: Annotated[DashboardContainer, Depends(get_container)],
) -> object:
    tracker = getattr(container, "case_tracking", None)
    if tracker is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="case_tracking_unavailable")
    try:
        return tracker.acknowledge_flag(case_id, user.user_id)
    except KeyError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="case_not_found") from None
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None