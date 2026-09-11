"""Dashboard service for communication status — Section 4.6."""
from __future__ import annotations

from typing import Any, Callable

from app.communication import (
    CommunicationService,
    CommunicationStatus,
    NookalCommunicationConfig,
)
from app.nookal_client import NookalClient


AuditFn = Callable[..., Any]


class CommunicationDashboardService:
    """
    Read-only communication status for the dashboard.

    Does NOT send messages. Exposes configuration and capability status.
    """

    def __init__(
        self,
        *,
        nookal: NookalClient,
        audit: AuditFn,
        config: NookalCommunicationConfig | None = None,
    ) -> None:
        self._nookal = nookal
        self._audit = audit
        self._comm = CommunicationService(nookal=nookal, config=config, audit=audit)

    def get_overview(
        self,
        *,
        actor: str,
        role: str,
        correlation_id: str,
    ) -> dict[str, Any]:
        """Return full communication status for the dashboard."""
        overview = self._comm.get_communication_overview()
        self._audit(
            actor=actor,
            action="dashboard.communication_status",
            target_type="system",
            target_id="communication",
            result="success",
            metadata={
                "correlation_id": correlation_id,
                "role": role,
            },
        )
        return overview

    def get_patient_readiness(
        self,
        patient_id: str,
        *,
        actor: str,
        role: str,
        correlation_id: str,
    ) -> dict[str, Any]:
        """Check whether a patient can receive SMS and/or email."""
        try:
            patient = self._nookal.get_patient(patient_id)
        except Exception:
            return {
                "patient_id": patient_id,
                "error": "patient_not_found",
                "sms_ready": False,
                "email_ready": False,
            }

        readiness = self._comm.check_patient_readiness(patient)
        self._audit(
            actor=actor,
            action="dashboard.patient_readiness_checked",
            target_type="system",
            target_id="communication",
            result="success",
            metadata={
                "correlation_id": correlation_id,
                "role": role,
                "patient_id": patient_id,
                "sms_ready": readiness.sms_ready,
                "email_ready": readiness.email_ready,
            },
        )
        p_phone = patient.phone or (patient.raw.get("Mobile") if isinstance(patient.raw, dict) else None) or (patient.raw.get("Telephone") if isinstance(patient.raw, dict) else None) or ""
        p_email = patient.email or (patient.raw.get("Email") if isinstance(patient.raw, dict) else None) or ""
        p_name = patient.first_name or patient.display_name or f"Patient #{readiness.patient_id}"

        return {
            "patient_id": readiness.patient_id,
            "patient_name": p_name,
            "phone": p_phone,
            "email": p_email,
            "has_phone": readiness.has_phone,
            "has_email": readiness.has_email,
            "sms_ready": readiness.sms_ready,
            "email_ready": readiness.email_ready,
            "sms_status": readiness.sms_status.value,
            "email_status": readiness.email_status.value,
        }

    def get_workflows(
        self,
        *,
        actor: str,
        role: str,
        correlation_id: str,
    ) -> list[dict[str, Any]]:
        """Return capability matrix for all workflows."""
        matrix = self._comm.get_capability_matrix()
        self._audit(
            actor=actor,
            action="dashboard.communication_workflows",
            target_type="system",
            target_id="communication",
            result="success",
            metadata={
                "correlation_id": correlation_id,
                "role": role,
            },
        )
        return matrix
