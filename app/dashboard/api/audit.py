from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends

from app.dashboard.auth import User
from app.dashboard.authorization import Permission
from app.dashboard.dependencies import (
    audit_viewer_service,
    get_correlation_id,
    require_permission,
)
from app.dashboard.schemas import AuditEventOut
from app.dashboard.services import AuditViewerService

router = APIRouter(prefix="/api/audit", tags=["audit"])


@router.get("", response_model=list[AuditEventOut])
def query_audit(
    user: Annotated[User, Depends(require_permission(Permission.AUDIT_VIEW))],
    svc: Annotated[AuditViewerService, Depends(audit_viewer_service)],
    correlation_id: Annotated[str, Depends(get_correlation_id)],
    actor: str | None = None,
    action: str | None = None,
    result: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    correlation_id_filter: str | None = None,
    limit: int = 200,
) -> list[AuditEventOut]:
    return svc.query(
        actor=user.user_id,
        role=user.role,
        correlation_id=correlation_id,
        filter_actor=actor,
        filter_action=action,
        filter_result=result,
        date_from=date_from,
        date_to=date_to,
        correlation_id_filter=correlation_id_filter,
        limit=min(limit, 500),
    )
