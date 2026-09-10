from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status

from app.dashboard.auth import User
from app.dashboard.authorization import Permission
from app.dashboard.dependencies import (
    get_correlation_id,
    referral_sync_service,
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


@router.post("/upload-report")
async def upload_referral_report(
    file: UploadFile = File(...),
    user: Annotated[User, Depends(require_permission(Permission.REFERRER_RESOLVE))] = None,
    sync_svc = Depends(referral_sync_service),
    correlation_id: Annotated[str, Depends(get_correlation_id)] = "upload",
) -> dict[str, Any]:
    from app.orchestration.referral_report import MalformedReportError
    try:
        content = await file.read()
        summary = sync_svc.sync_report_text(
            content.decode("utf-8-sig", errors="replace"),
            source_name=file.filename or "uploaded_report.csv",
            actor=user.user_id,
            role=user.role,
            correlation_id=correlation_id,
        )
        return {
            "status": "success",
            "filename": file.filename,
            "total_rows": summary.total_rows,
            "matched_exact": summary.matched_exact,
            "updated_changed": summary.updated_changed,
            "unchanged": summary.unchanged,
            "conflicts_queued": summary.conflicts_queued,
            "new_pending_queued": summary.new_pending_queued,
            "review_required_patients": summary.review_required_patients,
        }
    except MalformedReportError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to process referral report") from exc

