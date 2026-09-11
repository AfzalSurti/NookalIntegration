from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from app.dashboard.auth import User
from app.dashboard.authorization import Permission
from app.dashboard.dependencies import get_correlation_id, marketing_service, require_permission
from app.dashboard.schemas import ErrorOut
from app.dashboard.services.marketing import MarketingService

router = APIRouter(prefix="/api/marketing", tags=["marketing"])


@router.get("/lists", response_model=list[dict])
def list_marketing_lists(
    user: Annotated[User, Depends(require_permission(Permission.MARKETING_VIEW))],
    svc: Annotated[MarketingService, Depends(marketing_service)],
    correlation_id: Annotated[str, Depends(get_correlation_id)],
) -> list[dict]:
    return svc.list_lists(actor=user.user_id, role=user.role, correlation_id=correlation_id)


@router.post("/lists", response_model=dict)
def create_marketing_list(
    body: dict,
    user: Annotated[User, Depends(require_permission(Permission.MARKETING_MANAGE))],
    svc: Annotated[MarketingService, Depends(marketing_service)],
    correlation_id: Annotated[str, Depends(get_correlation_id)],
) -> dict:
    try:
        return svc.create_list(
            actor=user.user_id,
            role=user.role,
            correlation_id=correlation_id,
            body=body,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/lists/preview", response_model=dict)
def preview_marketing_list(
    body: dict,
    user: Annotated[User, Depends(require_permission(Permission.MARKETING_VIEW))],
    svc: Annotated[MarketingService, Depends(marketing_service)],
    correlation_id: Annotated[str, Depends(get_correlation_id)],
) -> dict:
    try:
        return svc.preview_audience(
            actor=user.user_id,
            role=user.role,
            correlation_id=correlation_id,
            body=body,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get("/campaigns", response_model=list[dict])
def list_campaigns(
    user: Annotated[User, Depends(require_permission(Permission.MARKETING_VIEW))],
    svc: Annotated[MarketingService, Depends(marketing_service)],
    correlation_id: Annotated[str, Depends(get_correlation_id)],
) -> list[dict]:
    return svc.list_campaigns(actor=user.user_id, role=user.role, correlation_id=correlation_id)


@router.post("/campaigns", response_model=dict)
def create_campaign(
    body: dict,
    user: Annotated[User, Depends(require_permission(Permission.MARKETING_MANAGE))],
    svc: Annotated[MarketingService, Depends(marketing_service)],
    correlation_id: Annotated[str, Depends(get_correlation_id)],
) -> dict:
    try:
        return svc.create_campaign(
            actor=user.user_id,
            role=user.role,
            correlation_id=correlation_id,
            body=body,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/campaigns/{campaign_id}/submit", response_model=dict)
def submit_campaign(
    campaign_id: str,
    user: Annotated[User, Depends(require_permission(Permission.MARKETING_MANAGE))],
    svc: Annotated[MarketingService, Depends(marketing_service)],
    correlation_id: Annotated[str, Depends(get_correlation_id)],
) -> dict:
    try:
        return svc.submit_campaign(
            campaign_id=campaign_id,
            actor=user.user_id,
            role=user.role,
            correlation_id=correlation_id,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/campaigns/{campaign_id}/approve", response_model=dict)
def approve_campaign(
    campaign_id: str,
    user: Annotated[User, Depends(require_permission(Permission.MARKETING_MANAGE))],
    svc: Annotated[MarketingService, Depends(marketing_service)],
    correlation_id: Annotated[str, Depends(get_correlation_id)],
) -> dict:
    try:
        return svc.approve_campaign(
            campaign_id=campaign_id,
            actor=user.user_id,
            role=user.role,
            correlation_id=correlation_id,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/campaigns/{campaign_id}/queue", response_model=dict)
def queue_campaign(
    campaign_id: str,
    user: Annotated[User, Depends(require_permission(Permission.MARKETING_MANAGE))],
    svc: Annotated[MarketingService, Depends(marketing_service)],
    correlation_id: Annotated[str, Depends(get_correlation_id)],
) -> dict:
    try:
        return svc.queue_campaign(
            campaign_id=campaign_id,
            actor=user.user_id,
            role=user.role,
            correlation_id=correlation_id,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/campaigns/{campaign_id}/send", response_model=dict)
def send_campaign(
    campaign_id: str,
    user: Annotated[User, Depends(require_permission(Permission.MARKETING_MANAGE))],
    svc: Annotated[MarketingService, Depends(marketing_service)],
    correlation_id: Annotated[str, Depends(get_correlation_id)],
) -> dict:
    try:
        return svc.send_campaign(
            campaign_id=campaign_id,
            actor=user.user_id,
            role=user.role,
            correlation_id=correlation_id,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
