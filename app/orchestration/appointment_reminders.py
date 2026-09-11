"""
Scheduled appointment reminders â€” fixed template, no LLM.

Section 4.6 refactor: checks whether Nookal handles reminders natively.
If Nookal native reminders are configured (default), the workflow defers
to Nookal rather than sending a duplicate application-side reminder.

Idempotency key: appt_{appointment_id}_reminder_{YYYY-MM-DD}
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any, ClassVar

from app.communication import (
    CommunicationOwnership,
    CommunicationService,
    CommunicationStatus,
    CommunicationWorkflowType,
    NookalCommunicationConfig,
)
from app.communication.nookal_native import appointment_should_skip_app_reminder
from app.messaging import Channel
from app.nookal_client import Appointment, PatientRef
from app.orchestration.base import BaseWorkflow
from app.orchestration.context import WorkflowContext
from app.orchestration.results import WorkflowErrorInfo, WorkflowResult, WorkflowStatus
from app.orchestration.retry import RetryPolicy, classify_batch_status
from app.shared.config import get_settings

# Statuses that should not receive a reminder.
_SKIP_STATUSES = frozenset({"cancelled", "canceled", "completed", "no_show", "no-show"})

DEFAULT_TEMPLATE_ID = "appointment_reminder"
DEFAULT_CHANNEL: Channel = "whatsapp"
DEFAULT_CALLER_ROLE = "system"


def reminder_idempotency_key(appointment_id: str, reminder_date: date) -> str:
    # Seed/live IDs may already be prefixed; normalize to one appt_ prefix.
    aid = appointment_id.removeprefix("appt_")
    return f"appt_{aid}_reminder_{reminder_date.isoformat()}"


def _format_date(d: date) -> str:
    return d.strftime("%d %b %Y")


def _format_time(appt: Appointment) -> str:
    return appt.starts_at.strftime("%H:%M")


class AppointmentRemindersWorkflow(BaseWorkflow):
    name: ClassVar[str] = "appointment_reminders"

    def execute(
        self,
        ctx: WorkflowContext,
        *,
        reminder_date: date | None = None,
        channel: Channel = DEFAULT_CHANNEL,
        template_id: str = DEFAULT_TEMPLATE_ID,
        caller_role: str = DEFAULT_CALLER_ROLE,
        **_extra: Any,
    ) -> WorkflowResult:
        """
        Send reminders for appointments on reminder_date (default: tomorrow vs clock).

        Section 4.6: If Nookal native reminders are configured, this workflow
        defers to Nookal rather than sending application-side duplicates.
        Processes each appointment independently â€” one failure does not stop others.
        """
        target_day = reminder_date or (ctx.clock.now().date() + timedelta(days=1))
        ctx.audit_event(
            "appointment_reminders.window",
            metadata={"reminder_date": target_day.isoformat(), "channel": channel},
        )

        # --- Section 4.6 duplicate prevention ---
        # Nookal natively handles SMS and Email reminders.
        # If channel is SMS or Email, defer to Nookal native unless app_reminders_enabled is True.
        # If channel is WhatsApp (non-native / application channel), proceed with app reminders.
        # Callers can also explicitly set app_reminders_enabled in extra parameters.
        try:
            settings = get_settings()
            comm_config = NookalCommunicationConfig(
                nookal_sms_enabled=settings.communication.nookal_sms_enabled,
                nookal_email_enabled=settings.communication.nookal_email_enabled,
                app_reminders_enabled=_extra.get(
                    "app_reminders_enabled",
                    settings.communication.app_reminders_enabled,
                ),
            )
        except Exception:
            comm_config = NookalCommunicationConfig(
                app_reminders_enabled=_extra.get("app_reminders_enabled", False),
            )

        comm_service = CommunicationService(nookal=ctx.nookal, config=comm_config)

        # Native channels defer to Nookal when app_reminders_enabled is False
        channel_is_native = channel in ("sms", "email")
        should_defer = channel_is_native and not comm_config.app_reminders_enabled

        if should_defer:
            ctx.audit_event(
                "appointment_reminders.nookal_native_deferred",
                metadata={
                    "reminder_date": target_day.isoformat(),
                    "channel": channel,
                    "ownership": CommunicationOwnership.NOOKAL_NATIVE_CONFIG_ONLY.value,
                    "reason": "nookal_handles_reminders_natively",
                },
            )
            return WorkflowResult.skipped(
                self.name,
                ctx.correlation_id,
                data={
                    "reminder_date": target_day.isoformat(),
                    "items": [],
                    "sent": 0,
                    "skipped": 0,
                    "failed": 0,
                    "blocked": 0,
                    "nookal_native_deferred": True,
                    "ownership": CommunicationOwnership.NOOKAL_NATIVE_CONFIG_ONLY.value,
                },
            )

        # --- Application-controlled reminders (only when explicitly enabled) ---
        appointments = ctx.nookal.list_appointments(on_date=target_day)
        eligible = [
            a
            for a in appointments
            if (a.status or "booked").lower() not in _SKIP_STATUSES
        ]

        if not eligible:
            ctx.audit_event(
                "appointment_reminders.empty",
                metadata={"reminder_date": target_day.isoformat(), "fetched": len(appointments)},
            )
            return WorkflowResult.skipped(
                self.name,
                ctx.correlation_id,
                data={
                    "reminder_date": target_day.isoformat(),
                    "items": [],
                    "sent": 0,
                    "skipped": 0,
                    "failed": 0,
                    "blocked": 0,
                },
            )

        items: list[dict[str, Any]] = []
        counts = {"sent": 0, "skipped": 0, "failed": 0, "blocked": 0}

        for appt in eligible:
            item = self._process_one(
                ctx,
                appt,
                reminder_date=target_day,
                channel=channel,
                template_id=template_id,
                caller_role=caller_role,
            )
            items.append(item)
            outcome = item["outcome"]
            if outcome in counts:
                counts[outcome] += 1

        status = classify_batch_status(
            sent=counts["sent"],
            skipped=counts["skipped"],
            failed=counts["failed"],
            blocked=counts["blocked"],
        )
        data = {
            "reminder_date": target_day.isoformat(),
            "items": items,
            **counts,
            "total": len(items),
        }
        error = None
        if status == WorkflowStatus.FAILED:
            error = WorkflowErrorInfo(
                code="all_reminders_failed",
                message="no reminders were sent successfully",
                details={"failed": counts["failed"], "blocked": counts["blocked"]},
            )
        elif status == WorkflowStatus.PARTIAL:
            error = WorkflowErrorInfo(
                code="partial_reminder_failures",
                message="some reminders failed or were blocked",
                details={"failed": counts["failed"], "blocked": counts["blocked"]},
            )
        elif status == WorkflowStatus.BLOCKED and counts["sent"] == 0 and counts["skipped"] == 0:
            error = WorkflowErrorInfo(
                code="kill_switch",
                message="all reminder sends blocked",
                details={"blocked": counts["blocked"]},
            )

        return WorkflowResult(
            workflow=self.name,
            status=status,
            correlation_id=ctx.correlation_id,
            data=data,
            error=error,
        )

    def _process_one(
        self,
        ctx: WorkflowContext,
        appt: Appointment,
        *,
        reminder_date: date,
        channel: Channel,
        template_id: str,
        caller_role: str,
    ) -> dict[str, Any]:
        key = reminder_idempotency_key(appt.appointment_id, reminder_date)
        base = {
            "appointment_id": appt.appointment_id,
            "patient_id": appt.patient_id,
            "idempotency_key": key,
        }

        # Section 4.6: skip if Nookal already sent a reminder for this appointment
        if appointment_should_skip_app_reminder(appt):
            ctx.audit_event(
                "appointment_reminders.nookal_already_handled",
                target_type="appointment",
                target_id=appt.appointment_id,
                result="success",
                metadata={"idempotency_key": key, "email_reminder_sent": appt.email_reminder_sent},
            )
            return {**base, "outcome": "skipped", "code": "nookal_already_handled"}

        try:
            patient = ctx.nookal.get_patient(appt.patient_id)
        except Exception as exc:
            ctx.audit_event(
                "appointment_reminders.patient_lookup_failed",
                target_type="appointment",
                target_id=appt.appointment_id,
                result="failure",
                metadata={"error_type": type(exc).__name__, "idempotency_key": key},
            )
            return {**base, "outcome": "failed", "code": "patient_lookup_failed"}

        contact = self._contact_for_channel(patient, channel)
        if not contact:
            ctx.audit_event(
                "appointment_reminders.missing_contact",
                target_type="appointment",
                target_id=appt.appointment_id,
                result="failure",
                metadata={"channel": channel, "idempotency_key": key},
            )
            return {**base, "outcome": "failed", "code": "missing_contact"}

        display = patient.display_name or "there"
        context = {
            "name": display,
            "date": _format_date(appt.starts_at.date()),
            "time": _format_time(appt),
        }

        try:
            settings = get_settings()
            max_attempts = int(getattr(settings.messaging, "max_retries", 3) or 3)
        except Exception:
            max_attempts = 3
        policy = RetryPolicy(max_attempts=max_attempts)

        msg = None
        for attempt in range(1, policy.max_attempts + 1):
            try:
                msg = ctx.messaging.send(
                    channel,
                    contact,
                    template_id,
                    context,
                    patient_id=appt.patient_id,
                    idempotency_key=key,
                    caller_role=caller_role,
                )
            except Exception as exc:
                if attempt >= policy.max_attempts:
                    ctx.audit_event(
                        "appointment_reminders.send_error",
                        target_type="message",
                        target_id=key,
                        result="failure",
                        metadata={"error_type": type(exc).__name__},
                    )
                    return {**base, "outcome": "failed", "code": "send_error"}
                ctx.audit_event(
                    "appointment_reminders.retry",
                    target_type="message",
                    target_id=key,
                    result="failure",
                    metadata={"attempt": attempt, "error_type": type(exc).__name__},
                )
                continue

            if msg.status == "failed":
                err_type = (msg.metadata or {}).get("error_type", "")
                transient = err_type in {"FakeTimeoutError", "NookalServerError", "TimeoutError"}
                if transient and attempt < policy.max_attempts:
                    ctx.audit_event(
                        "appointment_reminders.retry",
                        target_type="message",
                        target_id=key,
                        result="failure",
                        metadata={"attempt": attempt, "error_type": err_type},
                    )
                    continue
            break

        if msg is None:
            return {**base, "outcome": "failed", "code": "send_error"}

        if msg.status == "skipped":
            ctx.audit_event(
                "appointment_reminders.duplicate_skipped",
                target_type="message",
                target_id=key,
                result="success",
                metadata={"appointment_id": appt.appointment_id},
            )
            return {**base, "outcome": "skipped", "code": "already_sent"}

        if msg.status == "blocked":
            ctx.audit_event(
                "appointment_reminders.blocked",
                target_type="message",
                target_id=key,
                result="blocked",
                metadata={"appointment_id": appt.appointment_id},
            )
            return {**base, "outcome": "blocked", "code": "kill_switch"}

        if msg.status == "failed":
            ctx.audit_event(
                "appointment_reminders.send_failed",
                target_type="message",
                target_id=key,
                result="failure",
                metadata={
                    "appointment_id": appt.appointment_id,
                    "error_type": (msg.metadata or {}).get("error_type", "unknown"),
                },
            )
            return {**base, "outcome": "failed", "code": "send_failed"}

        ctx.audit_event(
            "appointment_reminders.sent",
            target_type="message",
            target_id=key,
            result="success",
            metadata={"appointment_id": appt.appointment_id, "channel": channel},
        )
        return {**base, "outcome": "sent", "message_id": msg.id}

    @staticmethod
    def _contact_for_channel(patient: PatientRef, channel: Channel) -> str | None:
        if channel in ("whatsapp", "sms"):
            return patient.phone or None
        if channel == "email":
            return patient.email or None
        return None

