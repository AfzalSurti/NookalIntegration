"""Communication status API routes — Section 4.6."""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status

from app.dashboard.auth import User
from app.dashboard.authorization import Permission
from app.dashboard.container import DashboardContainer
from app.dashboard.dependencies import (
    get_container,
    get_correlation_id,
    require_permission,
    communication_service,
)
from app.dashboard.schemas import SendMessageRequest, SendMessageResponse
from app.dashboard.services.communication import CommunicationDashboardService
from app.shared.exceptions import MessagingError, NookalNotFound, PermissionDenied

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


@router.post("/patient/{patient_id}/send", response_model=SendMessageResponse)
def send_patient_message(
    patient_id: str,
    req: SendMessageRequest,
    user: Annotated[User, Depends(require_permission(Permission.COMMUNICATION_VIEW))],
    container: Annotated[DashboardContainer, Depends(get_container)],
    correlation_id: Annotated[str, Depends(get_correlation_id)],
) -> SendMessageResponse:
    """Send an SMS, Email, or WhatsApp message to a specific patient."""
    try:
        patient = container.nookal.get_patient(patient_id)
    except NookalNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"Patient {patient_id} not found")
    except Exception as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to fetch patient: {exc}")

    if patient is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"Patient {patient_id} not found")

    contact = req.recipient_contact
    if not contact:
        if req.channel == "email":
            contact = patient.email
        else:
            contact = patient.phone

    if not contact:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"Patient has no registered {req.channel} contact. Please provide a recipient contact.",
        )

    patient_name = patient.first_name or patient.display_name or "Patient"
    context = {"name": patient_name, **req.context}

    if req.template_id == "direct_message":
        msg_text = (req.message or "").strip()
        if not msg_text:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail="Message text cannot be empty for direct message.",
            )
        context["message"] = msg_text

    import uuid
    idempotency_key = f"adhoc_{patient_id}_{req.channel}_{uuid.uuid4().hex[:10]}"

    try:
        msg = container.messaging.send(
            channel=req.channel,
            patient_contact=contact,
            template_id=req.template_id,
            context=context,
            patient_id=patient_id,
            idempotency_key=idempotency_key,
            caller_role=user.role,
            send_class="transactional",
        )
    except PermissionDenied as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except MessagingError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Messaging provider error: {exc}",
        ) from exc

    return SendMessageResponse(
        message_id=msg.id,
        status=msg.status,
        channel=msg.channel,
        recipient=contact,
        rendered_content=msg.rendered_content,
        sent_at=msg.sent_at,
        provider_ref=msg.provider_ref,
        idempotency_key=msg.idempotency_key,
    )

