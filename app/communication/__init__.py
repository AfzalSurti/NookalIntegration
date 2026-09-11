"""
Nookal-native communication service — Section 4.6.

Nookal does NOT expose any API endpoint for sending SMS or email.
All appointment communication is handled by Nookal's native automation
configured in the admin UI (Manage > Communications).

This package:
- Classifies workflows as NOOKAL_NATIVE / APPLICATION_CONTROLLED / UNSUPPORTED
- Observes Nookal's communication state via appointment fields
- Prevents duplicate sends when Nookal already handles a workflow
- Reports communication status for the dashboard

It does NOT:
- Invent Nookal API messaging endpoints
- Introduce third-party SMS/email providers
- Bypass Nookal's supported communication model
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Callable, Mapping

from app.nookal_client import NookalClient, PatientRef, Appointment


class CommunicationWorkflowType(StrEnum):
    """Appointment communication workflow types."""
    CONFIRMATION = "confirmation"
    REMINDER = "reminder"
    CANCELLATION = "cancellation"
    RESCHEDULE = "reschedule"
    NO_SHOW = "no_show"
    RECALL = "recall"


class CommunicationChannel(StrEnum):
    SMS = "sms"
    EMAIL = "email"


class CommunicationOwnership(StrEnum):
    """Who owns the communication workflow."""
    NOOKAL_NATIVE = "nookal_native"
    NOOKAL_NATIVE_CONFIG_ONLY = "nookal_native_config_only"
    APPLICATION_CONTROLLED = "application_controlled"
    OFFICIAL_API_UNSUPPORTED = "official_api_unsupported"
    NOT_AVAILABLE = "not_available"


class CommunicationStatus(StrEnum):
    """Status of a communication attempt or configuration."""
    ENABLED = "enabled"
    DISABLED = "disabled"
    MANAGED_IN_NOOKAL = "managed_in_nookal"
    STATUS_UNKNOWN = "status_unknown"
    SMS_SKIPPED_NO_PHONE = "sms_skipped_no_phone"
    EMAIL_SKIPPED_NO_EMAIL = "email_skipped_no_email"
    COMMUNICATION_SKIPPED = "communication_skipped"
    COMMUNICATION_FAILED = "communication_failed"
    NOOKAL_NATIVE_HANDLED = "nookal_native_handled"


@dataclass(frozen=True)
class WorkflowCapability:
    """Capability entry for one communication workflow."""
    workflow: CommunicationWorkflowType
    ownership: CommunicationOwnership
    sms_supported: bool
    email_supported: bool
    notes: str = ""


@dataclass(frozen=True)
class PatientCommunicationReadiness:
    """Whether a patient can receive communications."""
    patient_id: str
    has_phone: bool
    has_email: bool
    sms_ready: bool
    email_ready: bool
    sms_status: CommunicationStatus
    email_status: CommunicationStatus


@dataclass(frozen=True)
class AppointmentCommunicationStatus:
    """Status of communication for an individual appointment."""
    appointment_id: str
    email_reminder_sent: bool
    status: CommunicationStatus
    ownership: CommunicationOwnership
    message: str = ""


@dataclass(frozen=True)
class NookalCommunicationConfig:
    """Application-level communication configuration."""
    nookal_sms_enabled: bool = True
    nookal_email_enabled: bool = True
    app_reminders_enabled: bool = False  # Defer to Nookal native by default


# --- Capability Matrix (reflects verified Nookal research) ---

WORKFLOW_CAPABILITIES: tuple[WorkflowCapability, ...] = (
    WorkflowCapability(
        workflow=CommunicationWorkflowType.CONFIRMATION,
        ownership=CommunicationOwnership.NOOKAL_NATIVE_CONFIG_ONLY,
        sms_supported=True,
        email_supported=True,
        notes="Managed in Nookal: Manage > Communications > Appointment Confirmation",
    ),
    WorkflowCapability(
        workflow=CommunicationWorkflowType.REMINDER,
        ownership=CommunicationOwnership.NOOKAL_NATIVE_CONFIG_ONLY,
        sms_supported=True,
        email_supported=True,
        notes="Managed in Nookal: Manage > Communications > Reminders",
    ),
    WorkflowCapability(
        workflow=CommunicationWorkflowType.CANCELLATION,
        ownership=CommunicationOwnership.NOOKAL_NATIVE_CONFIG_ONLY,
        sms_supported=True,
        email_supported=True,
        notes="Managed in Nookal: Manage > Communications (if configured)",
    ),
    WorkflowCapability(
        workflow=CommunicationWorkflowType.RESCHEDULE,
        ownership=CommunicationOwnership.NOOKAL_NATIVE_CONFIG_ONLY,
        sms_supported=True,
        email_supported=True,
        notes="Rebook triggers new confirmation via Nookal native automation",
    ),
    WorkflowCapability(
        workflow=CommunicationWorkflowType.NO_SHOW,
        ownership=CommunicationOwnership.OFFICIAL_API_UNSUPPORTED,
        sms_supported=False,
        email_supported=False,
        notes="Nookal API does not expose no-show messaging",
    ),
    WorkflowCapability(
        workflow=CommunicationWorkflowType.RECALL,
        ownership=CommunicationOwnership.NOOKAL_NATIVE_CONFIG_ONLY,
        sms_supported=True,
        email_supported=True,
        notes="Managed in Nookal: Manage > Communications (if configured)",
    ),
)

WORKFLOW_CAPABILITY_MAP: dict[CommunicationWorkflowType, WorkflowCapability] = {
    wc.workflow: wc for wc in WORKFLOW_CAPABILITIES
}


AuditFn = Callable[..., Any]


class CommunicationService:
    """
    Central communication service — observes, classifies, prevents duplicates.

    Does NOT send messages. Nookal handles all native appointment SMS/email.
    """

    def __init__(
        self,
        *,
        nookal: NookalClient,
        config: NookalCommunicationConfig | None = None,
        audit: AuditFn | None = None,
    ) -> None:
        self._nookal = nookal
        self._config = config or NookalCommunicationConfig()
        self._audit = audit

    @property
    def config(self) -> NookalCommunicationConfig:
        return self._config

    def get_capability_matrix(self) -> list[dict[str, Any]]:
        """Return the full capability matrix for dashboard display."""
        return [
            {
                "workflow": wc.workflow.value,
                "ownership": wc.ownership.value,
                "sms_supported": wc.sms_supported,
                "email_supported": wc.email_supported,
                "notes": wc.notes,
            }
            for wc in WORKFLOW_CAPABILITIES
        ]

    def get_workflow_ownership(
        self, workflow: CommunicationWorkflowType,
    ) -> CommunicationOwnership:
        """Determine who owns a given workflow."""
        cap = WORKFLOW_CAPABILITY_MAP.get(workflow)
        if cap is None:
            return CommunicationOwnership.NOT_AVAILABLE
        return cap.ownership

    def is_nookal_native(self, workflow: CommunicationWorkflowType) -> bool:
        """Return True if Nookal handles this workflow natively."""
        ownership = self.get_workflow_ownership(workflow)
        return ownership in (
            CommunicationOwnership.NOOKAL_NATIVE,
            CommunicationOwnership.NOOKAL_NATIVE_CONFIG_ONLY,
        )

    def should_application_send(
        self, workflow: CommunicationWorkflowType,
    ) -> bool:
        """
        Return True only if the application should send for this workflow.

        Defaults to False for Nookal-native workflows to prevent duplicates.
        """
        if self.is_nookal_native(workflow):
            # Only send if app reminders are explicitly enabled AND this is a reminder
            if (
                workflow == CommunicationWorkflowType.REMINDER
                and self._config.app_reminders_enabled
            ):
                return True
            return False
        ownership = self.get_workflow_ownership(workflow)
        return ownership == CommunicationOwnership.APPLICATION_CONTROLLED

    def check_patient_readiness(
        self,
        patient: PatientRef,
    ) -> PatientCommunicationReadiness:
        """Check whether a patient can receive SMS and/or email."""
        has_phone = bool(patient.phone and patient.phone.strip())
        has_email = bool(patient.email and patient.email.strip())

        if has_phone:
            sms_status = CommunicationStatus.ENABLED if self._config.nookal_sms_enabled else CommunicationStatus.DISABLED
        else:
            sms_status = CommunicationStatus.SMS_SKIPPED_NO_PHONE

        if has_email:
            email_status = CommunicationStatus.ENABLED if self._config.nookal_email_enabled else CommunicationStatus.DISABLED
        else:
            email_status = CommunicationStatus.EMAIL_SKIPPED_NO_EMAIL

        return PatientCommunicationReadiness(
            patient_id=patient.patient_id,
            has_phone=has_phone,
            has_email=has_email,
            sms_ready=has_phone and self._config.nookal_sms_enabled,
            email_ready=has_email and self._config.nookal_email_enabled,
            sms_status=sms_status,
            email_status=email_status,
        )

    def get_appointment_communication_status(
        self,
        appointment: Appointment,
    ) -> AppointmentCommunicationStatus:
        """Evaluate communication status for an appointment."""
        reminder_sent = bool(appointment.email_reminder_sent)
        if reminder_sent:
            status = CommunicationStatus.NOOKAL_NATIVE_HANDLED
            msg = "Nookal native email reminder already sent"
        elif appointment.cancelled:
            status = CommunicationStatus.COMMUNICATION_SKIPPED
            msg = "Appointment is cancelled"
        elif appointment.arrived:
            status = CommunicationStatus.COMMUNICATION_SKIPPED
            msg = "Patient already arrived"
        elif appointment.dna:
            status = CommunicationStatus.COMMUNICATION_SKIPPED
            msg = "Patient did not attend (DNA)"
        else:
            status = CommunicationStatus.STATUS_UNKNOWN
            msg = "Pending or managed directly in Nookal"

        return AppointmentCommunicationStatus(
            appointment_id=appointment.appointment_id,
            email_reminder_sent=reminder_sent,
            status=status,
            ownership=CommunicationOwnership.NOOKAL_NATIVE_CONFIG_ONLY,
            message=msg,
        )

    def observe_appointment_communication(
        self,
        appointment: Appointment,
    ) -> dict[str, Any]:
        """
        Observe what Nookal has done for an appointment's communication.

        Uses the emailReminderSent field from the Nookal API response.
        Does NOT call any messaging endpoint — purely reads existing data.
        """
        return {
            "appointment_id": appointment.appointment_id,
            "email_reminder_sent": appointment.email_reminder_sent,
            "status": appointment.status,
            "cancelled": appointment.cancelled,
            "dna": appointment.dna,
            "arrived": appointment.arrived,
            "communication_observed": True,
        }

    def get_channel_status(self) -> dict[str, Any]:
        """Return SMS and email channel configuration status."""
        return {
            "sms": {
                "enabled": self._config.nookal_sms_enabled,
                "provider": "nookal_native",
                "status": CommunicationStatus.MANAGED_IN_NOOKAL.value,
                "configuration": "Managed in Nookal: Setup > Account > Billing > Credits (SMS)",
            },
            "email": {
                "enabled": self._config.nookal_email_enabled,
                "provider": "nookal_native",
                "status": CommunicationStatus.MANAGED_IN_NOOKAL.value,
                "configuration": "Managed in Nookal: Manage > Communications",
            },
        }

    def get_communication_overview(self) -> dict[str, Any]:
        """Full communication status for the dashboard."""
        channels = self.get_channel_status()
        sms_status = CommunicationStatus.ENABLED.value if self._config.nookal_sms_enabled else CommunicationStatus.DISABLED.value
        email_status = CommunicationStatus.ENABLED.value if self._config.nookal_email_enabled else CommunicationStatus.DISABLED.value
        return {
            "channels": channels,
            "nookal_sms": {
                "enabled": self._config.nookal_sms_enabled,
                "status": sms_status,
                "provider": "nookal_native",
                "sender_number": "Clinic Patient-Facing Number",
                "configuration": "Managed in Nookal: Setup > Account > Billing > Credits (SMS)",
            },
            "nookal_email": {
                "enabled": self._config.nookal_email_enabled,
                "status": email_status,
                "provider": "nookal_native",
                "sender_email": "Clinic Verified Domain",
                "configuration": "Managed in Nookal: Manage > Communications",
            },
            "workflows": self.get_capability_matrix(),
            "app_reminders_enabled": self._config.app_reminders_enabled,
            "third_party_sms_provider": "NOT_USED",
            "third_party_email_provider": "NOT_USED",
        }


    def _audit_event(self, action: str, **metadata: Any) -> None:
        if self._audit:
            self._audit(
                actor="communication",
                action=action,
                target_type="system",
                target_id="communication",
                result="success",
                metadata={k: v for k, v in metadata.items() if v is not None},
            )


__all__ = [
    "AppointmentCommunicationStatus",
    "CommunicationChannel",
    "CommunicationOwnership",
    "CommunicationService",
    "CommunicationStatus",
    "CommunicationWorkflowType",
    "NookalCommunicationConfig",
    "PatientCommunicationReadiness",
    "WorkflowCapability",
    "WORKFLOW_CAPABILITIES",
    "WORKFLOW_CAPABILITY_MAP",
]

