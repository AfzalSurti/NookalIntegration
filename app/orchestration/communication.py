"""
Communication check workflows — Section 4.6.

Deterministic workflows callable by OpenClaw, cron schedules, or dashboard.
No channel SDK or OpenClaw SDK dependencies. Read-only observation and validation.
"""
from __future__ import annotations

from typing import Any, ClassVar

from app.communication import (
    CommunicationService,
    NookalCommunicationConfig,
)
from app.orchestration.base import BaseWorkflow
from app.orchestration.context import WorkflowContext
from app.orchestration.identity import resolve_patient_by_phone
from app.orchestration.results import WorkflowResult


class CheckCommunicationConfigWorkflow(BaseWorkflow):
    """Checks and returns the current Nookal-native communication capability matrix."""

    name: ClassVar[str] = "check_communication_config"

    def execute(
        self,
        ctx: WorkflowContext,
        *,
        config: NookalCommunicationConfig | None = None,
        **_extra: Any,
    ) -> WorkflowResult:
        comm = CommunicationService(nookal=ctx.nookal, config=config, audit=ctx.audit_event)
        overview = comm.get_communication_overview()
        matrix = comm.get_capability_matrix()

        ctx.audit_event(
            "communication.config_checked",
            target_type="system",
            target_id="communication",
            result="success",
            metadata={
                "nookal_sms_enabled": overview["nookal_sms"]["enabled"],
                "nookal_email_enabled": overview["nookal_email"]["enabled"],
                "app_reminders_enabled": overview["app_reminders_enabled"],
            },
        )

        return WorkflowResult.success(
            self.name,
            ctx.correlation_id,
            data={
                "overview": overview,
                "workflows": matrix,
            },
        )



class CheckPatientReadinessWorkflow(BaseWorkflow):
    """
    Validates whether a patient has required contact information for Nookal communications.

    Accepts either patient_id or phone. Does NOT send any message.
    """

    name: ClassVar[str] = "check_patient_readiness"

    def execute(
        self,
        ctx: WorkflowContext,
        *,
        patient_id: str | None = None,
        phone: str | None = None,
        config: NookalCommunicationConfig | None = None,
        **_extra: Any,
    ) -> WorkflowResult:
        patient = None
        if patient_id:
            try:
                patient = ctx.nookal.get_patient(patient_id)
            except Exception as exc:
                ctx.audit_event(
                    "communication.patient_readiness_checked",
                    target_type="patient",
                    target_id=patient_id,
                    result="failure",
                    metadata={"error_type": type(exc).__name__},
                )
                return WorkflowResult.failed(
                    self.name,
                    ctx.correlation_id,
                    code="patient_not_found",
                    message="Patient not found in Nookal",
                    details={"patient_id": patient_id},
                )
        elif phone:
            resolved = resolve_patient_by_phone(ctx.nookal, phone)
            if resolved is None:
                return WorkflowResult.failed(
                    self.name,
                    ctx.correlation_id,
                    code="patient_not_found",
                    message="No patient matches phone number",
                )
            try:
                patient = ctx.nookal.get_patient(resolved.patient_id)
            except Exception as exc:
                return WorkflowResult.failed(
                    self.name,
                    ctx.correlation_id,
                    code="patient_not_found",
                    message="Patient record retrieval failed",
                    details={"error_type": type(exc).__name__},
                )
        else:
            return WorkflowResult.failed(
                self.name,
                ctx.correlation_id,
                code="missing_identifier",
                message="Either patient_id or phone must be provided",
            )

        comm = CommunicationService(nookal=ctx.nookal, config=config, audit=ctx.audit_event)
        readiness = comm.check_patient_readiness(patient)

        ctx.audit_event(
            "communication.patient_readiness_checked",
            target_type="patient",
            target_id=readiness.patient_id,
            result="success",
            metadata={
                "sms_ready": readiness.sms_ready,
                "email_ready": readiness.email_ready,
            },
        )

        return WorkflowResult.success(
            self.name,
            ctx.correlation_id,
            data={
                "patient_id": readiness.patient_id,
                "has_phone": readiness.has_phone,
                "has_email": readiness.has_email,
                "sms_ready": readiness.sms_ready,
                "email_ready": readiness.email_ready,
                "sms_status": readiness.sms_status.value,
                "email_status": readiness.email_status.value,
            },
        )


class CheckAppointmentCommunicationWorkflow(BaseWorkflow):
    """
    Checks appointment communication status: whether Nookal has handled reminders,
    ownership of the workflow, and patient readiness.
    """

    name: ClassVar[str] = "check_appointment_communication"

    def execute(
        self,
        ctx: WorkflowContext,
        *,
        appointment_id: str,
        config: NookalCommunicationConfig | None = None,
        **_extra: Any,
    ) -> WorkflowResult:
        try:
            appt = ctx.nookal.get_appointment(appointment_id)
        except Exception as exc:
            return WorkflowResult.failed(
                self.name,
                ctx.correlation_id,
                code="appointment_not_found",
                message="Appointment not found in Nookal",
                details={"appointment_id": appointment_id, "error_type": type(exc).__name__},
            )

        comm = CommunicationService(nookal=ctx.nookal, config=config, audit=ctx.audit_event)
        appt_status = comm.get_appointment_communication_status(appt)

        ctx.audit_event(
            "communication.status_observed",
            target_type="appointment",
            target_id=appointment_id,
            result="success",
            metadata={
                "email_reminder_sent": appt_status.email_reminder_sent,
                "status": appt_status.status.value,
            },
        )

        return WorkflowResult.success(
            self.name,
            ctx.correlation_id,
            data={
                "appointment_id": appt_status.appointment_id,
                "email_reminder_sent": appt_status.email_reminder_sent,
                "status": appt_status.status.value,
                "ownership": appt_status.ownership.value,
                "message": appt_status.message,
            },
        )

