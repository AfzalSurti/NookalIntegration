"""
Templates Management API.

Allows authorized clinic staff and practitioners to:
- List all available templates across Communication (SMS/Email), Clinical Letters, and Marketing
- Inspect template body, metadata, and placeholders
- Live preview template rendering
- Safely update template wording and subject lines
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.dashboard.authorization.permissions import Permission
from app.dashboard.authorization.policy import AuthorizationPolicy
from app.dashboard.dependencies import (
    get_container,
    get_correlation_id,
    require_permission,
    require_user,
    template_service,
)
from app.dashboard.auth import User
from app.dashboard.container import DashboardContainer
from app.dashboard.services.templates import TemplateManagementService

router = APIRouter(prefix="/api/templates", tags=["templates"])


class UpdateTemplateRequest(BaseModel):
    body: str = Field(..., min_length=1, description="Template body text with {placeholder} tokens")
    subject: str | None = Field(None, description="Subject line for email / campaign templates")
    name: str | None = Field(None, description="Optional updated friendly name")


class PreviewTemplateRequest(BaseModel):
    category: str = Field(..., description="communication | letters | marketing")
    template_id: str = Field(..., description="Identifier of the template")
    override_body: str | None = Field(None, description="Optional unsaved body to preview in real-time")
    sample_context: dict[str, Any] | None = Field(None, description="Optional custom key-value pairs to preview")


class CreateMarketingTemplateRequest(BaseModel):
    id: str = Field(..., min_length=3, pattern=r"^[a-zA-Z0-9_-]+$", description="Slug id for the template")
    name: str = Field(..., min_length=2, description="Display name for the campaign template")
    subject: str = Field(..., min_length=2, description="Email subject line")
    body: str = Field(..., min_length=5, description="Email body content")


@router.get("", response_model=None)
async def list_templates(
    category: str | None = None,
    user: Annotated[User, Depends(require_permission(Permission.COMMUNICATION_VIEW))] = None,
    svc: Annotated[TemplateManagementService, Depends(template_service)] = None,
) -> list[dict[str, Any]]:
    """List all templates or filter by category ('communication', 'letters', 'marketing')."""
    templates = svc.list_all(category=category)
    return [t.to_dict() for t in templates]


@router.get("/{category}/{template_id}", response_model=None)
async def get_template(
    category: str,
    template_id: str,
    user: Annotated[User, Depends(require_permission(Permission.COMMUNICATION_VIEW))] = None,
    svc: Annotated[TemplateManagementService, Depends(template_service)] = None,
) -> dict[str, Any]:
    """Get single template details, body, and placeholders."""
    try:
        t = svc.get(category, template_id)
        return t.to_dict()
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.post("/preview", response_model=None)
async def preview_template(
    req: PreviewTemplateRequest,
    user: Annotated[User, Depends(require_permission(Permission.COMMUNICATION_VIEW))] = None,
    svc: Annotated[TemplateManagementService, Depends(template_service)] = None,
) -> dict[str, Any]:
    """Generate a live preview of a template with sample or custom context."""
    try:
        return svc.preview(
            category=req.category,
            template_id=req.template_id,
            override_body=req.override_body,
            sample_context=req.sample_context,
        )
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.put("/{category}/{template_id}", response_model=None)
async def update_template(
    category: str,
    template_id: str,
    req: UpdateTemplateRequest,
    user: Annotated[User, Depends(require_user)] = None,
    container: Annotated[DashboardContainer, Depends(get_container)] = None,
    svc: Annotated[TemplateManagementService, Depends(template_service)] = None,
    correlation_id: Annotated[str, Depends(get_correlation_id)] = "",
) -> dict[str, Any]:
    """
    Safely update a template's wording, subject, and placeholders.
    Enforces role authorization per category and validates placeholder integrity.
    """
    policy: AuthorizationPolicy = container.policy
    cat = category.lower().strip()

    # Permission checks:
    if cat == "communication":
        # Admin or Owner or COMMUNICATION_MANAGE
        if user.role not in ("admin", "owner"):
            try:
                policy.require(user.role, Permission.COMMUNICATION_MANAGE)
            except Exception:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden: communication template edit requires manage role") from None
    elif cat == "letters":
        # Practitioners, Admins, and Owners can edit clinical letter templates
        if user.role not in ("practitioner", "admin", "owner"):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden: clinical templates require practitioner or admin role")
    elif cat == "marketing":
        # Admin or Owner or MARKETING_MANAGE
        if user.role not in ("admin", "owner"):
            try:
                policy.require(user.role, Permission.MARKETING_MANAGE)
            except Exception:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden: marketing template edit requires manage role") from None
    else:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"invalid category: {category}")

    try:
        updated = svc.update(
            category=cat,
            template_id=template_id,
            body=req.body,
            subject=req.subject,
            name=req.name,
            actor=user.user_id,
        )

        # Audit logging
        if container.audit:
            container.audit(
                actor=user.user_id,
                action="template.update",
                target_type="system",
                target_id=template_id,
                result="success",
                metadata={
                    "category": cat,
                    "char_count": len(req.body),
                },
            )

        return {
            "success": True,
            "message": f"Template '{template_id}' successfully updated.",
            "template": updated.to_dict(),
        }
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/marketing", response_model=None)
async def create_marketing_template(
    req: CreateMarketingTemplateRequest,
    user: Annotated[User, Depends(require_permission(Permission.MARKETING_MANAGE))] = None,
    container: Annotated[DashboardContainer, Depends(get_container)] = None,
    svc: Annotated[TemplateManagementService, Depends(template_service)] = None,
    correlation_id: Annotated[str, Depends(get_correlation_id)] = "",
) -> dict[str, Any]:
    """Create a new marketing campaign template."""
    from app.marketing.models import CampaignTemplate
    from datetime import datetime
    import re

    placeholders = frozenset(re.findall(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", req.body))
    tpl = CampaignTemplate(
        id=req.id,
        name=req.name,
        subject=req.subject,
        body=req.body,
        placeholders=placeholders,
        version="1.0",
        created_by=user.user_id,
        created_at=datetime.now().isoformat(),
    )
    svc._marketing_store.save(tpl)

    if container.audit:
        container.audit(
            actor=user.user_id,
            action="template.create",
            target_type="system",
            target_id=req.id,
            result="success",
            metadata={"category": "marketing"},
        )

    return {
        "success": True,
        "message": f"Marketing template '{req.name}' created.",
        "template": svc.get("marketing", req.id).to_dict(),
    }
