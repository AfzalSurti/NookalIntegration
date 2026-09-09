"""
WhatsApp-driven appointment check / reschedule / cancel / create workflows.

LLM is used only for intent parsing. Writes require explicit confirmation.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, ClassVar

from app.orchestration.base import BaseWorkflow
from app.orchestration.context import WorkflowContext
from app.orchestration.identity import (
    is_affirmative,
    is_negative,
    resolve_patient_by_phone,
)
from app.orchestration.pending_actions import PendingActionStore
from app.orchestration.results import WorkflowResult
from app.shared.exceptions import KillSwitchActive, NookalError

# Deterministic clinic policy — not an LLM decision.
CANCELLATION_WINDOW_HOURS = 24
CONFIRM_TEMPLATE = "reschedule_confirm_prompt"


def _require_llm(ctx: WorkflowContext) -> Any:
    if ctx.llm is None:
        raise RuntimeError("llm_required")
    return ctx.llm


def _require_pending(ctx: WorkflowContext) -> PendingActionStore:
    store = ctx.pending_actions
    if store is None or not isinstance(store, PendingActionStore):
        raise RuntimeError("pending_actions_required")
    return store


def _parse_intent(ctx: WorkflowContext, message: str) -> Any:
    llm = _require_llm(ctx)
    return llm.parse_intent(message)


def _identity_or_result(
    ctx: WorkflowContext,
    workflow: str,
    phone: str,
) -> tuple[Any, WorkflowResult | None]:
    resolution = resolve_patient_by_phone(ctx.nookal, phone)
    ctx.audit_event(
        f"{workflow}.identity",
        target_type="patient_record",
        target_id="phone_lookup",
        result="success" if resolution.status == "matched" else "failure",
        metadata={"status": resolution.status, "match_count": resolution.match_count},
    )
    if resolution.status == "not_found":
        return None, WorkflowResult.needs_human(
            workflow,
            ctx.correlation_id,
            code="patient_not_found",
            message="no patient matched phone",
        )
    if resolution.status == "ambiguous":
        return None, WorkflowResult.needs_human(
            workflow,
            ctx.correlation_id,
            code="patient_ambiguous",
            message="multiple patients matched phone",
            details={"match_count": resolution.match_count},
        )
    return resolution.patient, None


def _parse_when(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=__import__("datetime").timezone.utc)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


class CheckAppointmentWorkflow(BaseWorkflow):
    name: ClassVar[str] = "check_appointment"

    def execute(
        self,
        ctx: WorkflowContext,
        *,
        phone: str,
        message: str,
        **_extra: Any,
    ) -> WorkflowResult:
        try:
            intent = _parse_intent(ctx, message)
        except Exception as exc:
            ctx.audit_event(
                "check_appointment.llm_failed",
                result="failure",
                metadata={"error_type": type(exc).__name__},
            )
            return WorkflowResult.failed(
                self.name,
                ctx.correlation_id,
                code="llm_failure",
                message="intent parsing failed",
                details={"error_type": type(exc).__name__},
            )

        if getattr(intent, "confidence", "low") != "high" or getattr(intent, "intent", "other") != "check_appointment":
            ctx.audit_event(
                "check_appointment.intent_rejected",
                metadata={
                    "intent": getattr(intent, "intent", "other"),
                    "confidence": getattr(intent, "confidence", "low"),
                },
            )
            return WorkflowResult.needs_human(
                self.name,
                ctx.correlation_id,
                code="intent_not_actionable",
                message="intent ambiguous or not check_appointment",
                data={
                    "intent": getattr(intent, "intent", "other"),
                    "confidence": getattr(intent, "confidence", "low"),
                },
            )

        patient, err = _identity_or_result(ctx, self.name, phone)
        if err:
            return err

        appts = ctx.nookal.list_appointments(patient_id=patient.patient_id)
        # Upcoming / active only — minimum necessary fields.
        now = ctx.clock.now()
        summaries = []
        for a in sorted(appts, key=lambda x: x.starts_at):
            if a.starts_at < now and (a.status or "").lower() in {"completed", "cancelled", "canceled"}:
                continue
            summaries.append(
                {
                    "appointment_id": a.appointment_id,
                    "starts_at": a.starts_at.isoformat(),
                    "status": a.status or "booked",
                }
            )

        ctx.audit_event(
            "check_appointment.lookup",
            target_type="appointment",
            target_id=patient.patient_id,
            metadata={"count": len(summaries)},
        )
        if not summaries:
            return WorkflowResult.success(
                self.name,
                ctx.correlation_id,
                data={"patient_id": patient.patient_id, "appointments": []},
            )
        return WorkflowResult.success(
            self.name,
            ctx.correlation_id,
            data={"patient_id": patient.patient_id, "appointments": summaries},
        )


class RescheduleAppointmentWorkflow(BaseWorkflow):
    name: ClassVar[str] = "reschedule_appointment"

    def execute(
        self,
        ctx: WorkflowContext,
        *,
        phone: str,
        message: str,
        confirmation_text: str | None = None,
        pending_action_id: str | None = None,
        **_extra: Any,
    ) -> WorkflowResult:
        store = _require_pending(ctx)

        # Confirmation path
        if confirmation_text is not None or pending_action_id is not None:
            return self._confirm(ctx, store, phone, confirmation_text, pending_action_id)

        try:
            intent = _parse_intent(ctx, message)
        except Exception as exc:
            return WorkflowResult.failed(
                self.name,
                ctx.correlation_id,
                code="llm_failure",
                message="intent parsing failed",
                details={"error_type": type(exc).__name__},
            )

        if getattr(intent, "confidence", "low") != "high" or getattr(intent, "intent", "") != "reschedule_appointment":
            return WorkflowResult.needs_human(
                self.name,
                ctx.correlation_id,
                code="intent_not_actionable",
                message="intent ambiguous or not reschedule",
            )

        fields = getattr(intent, "extracted_fields", {}) or {}
        new_when = _parse_when(fields.get("new_starts_at") or fields.get("new_time") or fields.get("datetime"))
        if new_when is None:
            return WorkflowResult.needs_human(
                self.name,
                ctx.correlation_id,
                code="missing_new_time",
                message="new appointment time not explicitly provided",
            )

        patient, err = _identity_or_result(ctx, self.name, phone)
        if err:
            return err

        appt = self._resolve_appointment(ctx, patient.patient_id, fields)
        if isinstance(appt, WorkflowResult):
            return appt

        if not self._slot_available(ctx, new_when, exclude_appointment_id=appt.appointment_id):
            ctx.audit_event(
                "reschedule_appointment.slot_unavailable",
                target_type="appointment",
                target_id=appt.appointment_id,
                result="failure",
            )
            return WorkflowResult.failed(
                self.name,
                ctx.correlation_id,
                code="slot_unavailable",
                message="requested slot is not available",
                details={"appointment_id": appt.appointment_id},
            )

        pending = store.create(
            action="reschedule",
            patient_id=patient.patient_id,
            phone=phone,
            payload={
                "appointment_id": appt.appointment_id,
                "new_starts_at": new_when.isoformat(),
                "current_starts_at": appt.starts_at.isoformat(),
            },
            correlation_id=ctx.correlation_id,
        )
        # Prompt patient — template send (not generative clinical content).
        current_slot = appt.starts_at.strftime("%a %d %b %H:%M")
        new_slot = new_when.strftime("%a %d %b %H:%M")
        display = patient.display_name or "there"
        if patient.phone:
            ctx.messaging.send(
                "whatsapp",
                patient.phone,
                CONFIRM_TEMPLATE,
                {"name": display, "current_slot": current_slot, "new_slot": new_slot},
                patient_id=patient.patient_id,
                idempotency_key=f"resched_prompt_{pending.id}",
                caller_role="system",
            )
        ctx.audit_event(
            "reschedule_appointment.awaiting_confirmation",
            target_type="appointment",
            target_id=appt.appointment_id,
            metadata={"pending_action_id": pending.id},
        )
        return WorkflowResult.needs_confirmation(
            self.name,
            ctx.correlation_id,
            data={
                "pending_action_id": pending.id,
                "appointment_id": appt.appointment_id,
                "patient_id": patient.patient_id,
                "new_starts_at": new_when.isoformat(),
            },
        )

    def _confirm(
        self,
        ctx: WorkflowContext,
        store: PendingActionStore,
        phone: str,
        confirmation_text: str | None,
        pending_action_id: str | None,
    ) -> WorkflowResult:
        text = confirmation_text or ""
        if is_negative(text):
            ctx.audit_event("reschedule_appointment.declined", metadata={})
            return WorkflowResult.skipped(
                self.name,
                ctx.correlation_id,
                data={"reason": "declined"},
            )
        if not is_affirmative(text) and pending_action_id is None:
            return WorkflowResult.needs_human(
                self.name,
                ctx.correlation_id,
                code="confirmation_unclear",
                message="confirmation text not recognized",
            )

        action = None
        if pending_action_id:
            action = store.consume(pending_action_id)
        else:
            opens = store.find_open(phone=phone, action="reschedule")
            if len(opens) != 1:
                return WorkflowResult.needs_human(
                    self.name,
                    ctx.correlation_id,
                    code="pending_ambiguous",
                    message="could not uniquely identify pending reschedule",
                )
            action = store.consume(opens[0].id)

        if action is None:
            return WorkflowResult.failed(
                self.name,
                ctx.correlation_id,
                code="pending_expired_or_replay",
                message="pending confirmation missing, expired, or already used",
            )
        if action.phone != phone or action.patient_id is None:
            return WorkflowResult.failed(
                self.name,
                ctx.correlation_id,
                code="patient_mismatch",
                message="confirmation phone does not match pending action",
            )

        new_when = _parse_when(action.payload.get("new_starts_at"))
        appt_id = str(action.payload["appointment_id"])
        try:
            updated = ctx.nookal.update_appointment(appt_id, starts_at=new_when)
        except KillSwitchActive:
            raise
        except NookalError as exc:
            ctx.audit_event(
                "reschedule_appointment.write_failed",
                target_type="appointment",
                target_id=appt_id,
                result="failure",
                metadata={"error_type": type(exc).__name__},
            )
            return WorkflowResult.failed(
                self.name,
                ctx.correlation_id,
                code="nookal_write_failed",
                message="failed to update appointment",
                details={"error_type": type(exc).__name__, "appointment_id": appt_id},
            )

        ctx.audit_event(
            "reschedule_appointment.updated",
            target_type="appointment",
            target_id=appt_id,
            metadata={"pending_action_id": action.id},
        )
        return WorkflowResult.success(
            self.name,
            ctx.correlation_id,
            data={
                "appointment_id": updated.appointment_id,
                "patient_id": updated.patient_id,
                "starts_at": updated.starts_at.isoformat(),
            },
        )

    def _resolve_appointment(self, ctx: WorkflowContext, patient_id: str, fields: dict) -> Any:
        appt_id = fields.get("appointment_id") or fields.get("appointment_ref")
        if appt_id:
            try:
                appt = ctx.nookal.get_appointment(str(appt_id))
            except NookalError:
                return WorkflowResult.failed(
                    self.name,
                    ctx.correlation_id,
                    code="appointment_not_found",
                    message="appointment not found",
                )
            if appt.patient_id != patient_id:
                return WorkflowResult.failed(
                    self.name,
                    ctx.correlation_id,
                    code="patient_mismatch",
                    message="appointment does not belong to patient",
                )
            return appt

        upcoming = [
            a
            for a in ctx.nookal.list_appointments(patient_id=patient_id)
            if (a.status or "booked").lower() not in {"cancelled", "canceled", "completed"}
            and a.starts_at >= ctx.clock.now()
        ]
        if len(upcoming) == 0:
            return WorkflowResult.failed(
                self.name,
                ctx.correlation_id,
                code="no_appointment",
                message="no upcoming appointment to reschedule",
            )
        if len(upcoming) > 1:
            return WorkflowResult.needs_human(
                self.name,
                ctx.correlation_id,
                code="appointment_ambiguous",
                message="multiple upcoming appointments",
                details={"count": len(upcoming)},
            )
        return upcoming[0]

    def _slot_available(
        self,
        ctx: WorkflowContext,
        when: datetime,
        *,
        exclude_appointment_id: str | None = None,
    ) -> bool:
        # Conflict with existing appointments (simple deterministic rule).
        for a in ctx.nookal.list_appointments(on_date=when.date()):
            if exclude_appointment_id and a.appointment_id == exclude_appointment_id:
                continue
            if (a.status or "").lower() in {"cancelled", "canceled"}:
                continue
            if a.starts_at == when:
                return False
        list_fn = getattr(ctx.nookal, "list_open_slots", None)
        if callable(list_fn):
            opens = list_fn(on_date=when.date())
            if opens and when not in opens:
                # If the mock publishes open slots, require membership.
                return False
        return True


class CancelAppointmentWorkflow(BaseWorkflow):
    name: ClassVar[str] = "cancel_appointment"

    def execute(
        self,
        ctx: WorkflowContext,
        *,
        phone: str,
        message: str,
        confirmation_text: str | None = None,
        pending_action_id: str | None = None,
        cancellation_window_hours: int = CANCELLATION_WINDOW_HOURS,
        **_extra: Any,
    ) -> WorkflowResult:
        store = _require_pending(ctx)
        if confirmation_text is not None or pending_action_id is not None:
            return self._confirm(ctx, store, phone, confirmation_text, pending_action_id)

        try:
            intent = _parse_intent(ctx, message)
        except Exception as exc:
            return WorkflowResult.failed(
                self.name,
                ctx.correlation_id,
                code="llm_failure",
                message="intent parsing failed",
                details={"error_type": type(exc).__name__},
            )

        if getattr(intent, "confidence", "low") != "high" or getattr(intent, "intent", "") != "cancel_appointment":
            return WorkflowResult.needs_human(
                self.name,
                ctx.correlation_id,
                code="intent_not_actionable",
                message="intent ambiguous or not cancel",
            )

        patient, err = _identity_or_result(ctx, self.name, phone)
        if err:
            return err

        fields = getattr(intent, "extracted_fields", {}) or {}
        appt = RescheduleAppointmentWorkflow()._resolve_appointment(ctx, patient.patient_id, fields)
        # Reuse resolve but fix workflow name on results — override by re-checking
        if isinstance(appt, WorkflowResult):
            # Re-emit with cancel workflow name
            return WorkflowResult(
                workflow=self.name,
                status=appt.status,
                correlation_id=ctx.correlation_id,
                data=appt.data,
                error=appt.error,
            )

        status = (appt.status or "booked").lower()
        if status in {"cancelled", "canceled"}:
            return WorkflowResult.skipped(
                self.name,
                ctx.correlation_id,
                data={"appointment_id": appt.appointment_id, "reason": "already_cancelled"},
            )
        if status == "completed":
            return WorkflowResult.failed(
                self.name,
                ctx.correlation_id,
                code="already_completed",
                message="completed appointments cannot be cancelled",
                details={"appointment_id": appt.appointment_id},
            )

        hours_to_start = (appt.starts_at - ctx.clock.now()).total_seconds() / 3600.0
        if hours_to_start < cancellation_window_hours:
            ctx.audit_event(
                "cancel_appointment.outside_window",
                target_type="appointment",
                target_id=appt.appointment_id,
                result="failure",
                metadata={"window_hours": cancellation_window_hours},
            )
            return WorkflowResult.failed(
                self.name,
                ctx.correlation_id,
                code="outside_cancellation_window",
                message="appointment is inside the cancellation window",
                details={
                    "appointment_id": appt.appointment_id,
                    "window_hours": cancellation_window_hours,
                },
            )

        pending = store.create(
            action="cancel",
            patient_id=patient.patient_id,
            phone=phone,
            payload={"appointment_id": appt.appointment_id},
            correlation_id=ctx.correlation_id,
        )
        ctx.audit_event(
            "cancel_appointment.awaiting_confirmation",
            target_type="appointment",
            target_id=appt.appointment_id,
            metadata={"pending_action_id": pending.id},
        )
        return WorkflowResult.needs_confirmation(
            self.name,
            ctx.correlation_id,
            data={
                "pending_action_id": pending.id,
                "appointment_id": appt.appointment_id,
                "patient_id": patient.patient_id,
            },
        )

    def _confirm(
        self,
        ctx: WorkflowContext,
        store: PendingActionStore,
        phone: str,
        confirmation_text: str | None,
        pending_action_id: str | None,
    ) -> WorkflowResult:
        text = confirmation_text or ""
        if is_negative(text):
            return WorkflowResult.skipped(
                self.name, ctx.correlation_id, data={"reason": "declined"}
            )
        if not is_affirmative(text) and not pending_action_id:
            return WorkflowResult.needs_human(
                self.name,
                ctx.correlation_id,
                code="confirmation_unclear",
                message="confirmation text not recognized",
            )

        if pending_action_id:
            action = store.consume(pending_action_id)
        else:
            opens = store.find_open(phone=phone, action="cancel")
            if len(opens) != 1:
                return WorkflowResult.needs_human(
                    self.name,
                    ctx.correlation_id,
                    code="pending_ambiguous",
                    message="could not uniquely identify pending cancel",
                )
            action = store.consume(opens[0].id)

        if action is None:
            return WorkflowResult.failed(
                self.name,
                ctx.correlation_id,
                code="pending_expired_or_replay",
                message="pending confirmation missing, expired, or already used",
            )
        if action.phone != phone:
            return WorkflowResult.failed(
                self.name,
                ctx.correlation_id,
                code="patient_mismatch",
                message="confirmation phone does not match pending action",
            )

        appt_id = str(action.payload["appointment_id"])
        try:
            if hasattr(ctx.nookal, "cancel_appointment"):
                updated = ctx.nookal.cancel_appointment(appt_id, patient_id=action.patient_id)
            else:
                updated = ctx.nookal.update_appointment(appt_id, status="cancelled")
        except KillSwitchActive:
            raise
        except NookalError as exc:
            return WorkflowResult.failed(
                self.name,
                ctx.correlation_id,
                code="nookal_write_failed",
                message="failed to cancel appointment",
                details={"error_type": type(exc).__name__, "appointment_id": appt_id},
            )

        ctx.audit_event(
            "cancel_appointment.updated",
            target_type="appointment",
            target_id=appt_id,
            metadata={"pending_action_id": action.id},
        )
        return WorkflowResult.success(
            self.name,
            ctx.correlation_id,
            data={
                "appointment_id": updated.appointment_id,
                "patient_id": updated.patient_id,
                "status": updated.status,
            },
        )


class CreateAppointmentWorkflow(BaseWorkflow):
    name: ClassVar[str] = "create_appointment"

    def execute(
        self,
        ctx: WorkflowContext,
        *,
        phone: str,
        message: str,
        confirmation_text: str | None = None,
        pending_action_id: str | None = None,
        **_extra: Any,
    ) -> WorkflowResult:
        store = _require_pending(ctx)
        if confirmation_text is not None or pending_action_id is not None:
            return self._confirm(ctx, store, phone, confirmation_text, pending_action_id)

        try:
            intent = _parse_intent(ctx, message)
        except Exception as exc:
            return WorkflowResult.failed(
                self.name,
                ctx.correlation_id,
                code="llm_failure",
                message="intent parsing failed",
                details={"error_type": type(exc).__name__},
            )

        if getattr(intent, "confidence", "low") != "high" or getattr(intent, "intent", "") != "create_appointment":
            return WorkflowResult.needs_human(
                self.name,
                ctx.correlation_id,
                code="intent_not_actionable",
                message="intent ambiguous or not create",
            )

        fields = getattr(intent, "extracted_fields", {}) or {}
        when = _parse_when(fields.get("starts_at") or fields.get("datetime") or fields.get("new_time"))
        if when is None:
            # Missing date/time — ask, do not invent.
            missing = []
            if not (fields.get("date") or fields.get("starts_at") or fields.get("datetime")):
                missing.append("date")
            if not (fields.get("time") or fields.get("starts_at") or fields.get("datetime")):
                missing.append("time")
            return WorkflowResult.needs_human(
                self.name,
                ctx.correlation_id,
                code="missing_required_fields",
                message="required booking fields missing",
                details={"missing": missing or ["starts_at"]},
            )

        patient, err = _identity_or_result(ctx, self.name, phone)
        if err:
            return err

        list_fn = getattr(ctx.nookal, "list_open_slots", None)
        catalog = getattr(ctx.nookal, "open_slots", None)
        if callable(list_fn) and catalog:
            open_slots: list[datetime] = list_fn(on_date=when.date())
            if when not in open_slots:
                alts = [s.isoformat() for s in open_slots[:5]]
                if not alts:
                    return WorkflowResult.failed(
                        self.name,
                        ctx.correlation_id,
                        code="no_availability",
                        message="no open slots on requested day",
                    )
                return WorkflowResult.needs_confirmation(
                    self.name,
                    ctx.correlation_id,
                    data={
                        "reason": "slot_not_open",
                        "requested": when.isoformat(),
                        "available_slots": alts,
                        "patient_id": patient.patient_id,
                    },
                )

        pending = store.create(
            action="create",
            patient_id=patient.patient_id,
            phone=phone,
            payload={"starts_at": when.isoformat(), "status": "booked"},
            correlation_id=ctx.correlation_id,
        )
        ctx.audit_event(
            "create_appointment.awaiting_confirmation",
            target_type="appointment",
            target_id=patient.patient_id,
            metadata={"pending_action_id": pending.id},
        )
        return WorkflowResult.needs_confirmation(
            self.name,
            ctx.correlation_id,
            data={
                "pending_action_id": pending.id,
                "patient_id": patient.patient_id,
                "starts_at": when.isoformat(),
            },
        )

    def _confirm(
        self,
        ctx: WorkflowContext,
        store: PendingActionStore,
        phone: str,
        confirmation_text: str | None,
        pending_action_id: str | None,
    ) -> WorkflowResult:
        text = confirmation_text or ""
        if is_negative(text):
            return WorkflowResult.skipped(
                self.name, ctx.correlation_id, data={"reason": "declined"}
            )
        if not is_affirmative(text) and not pending_action_id:
            return WorkflowResult.needs_human(
                self.name,
                ctx.correlation_id,
                code="confirmation_unclear",
                message="confirmation text not recognized",
            )

        if pending_action_id:
            action = store.consume(pending_action_id)
        else:
            opens = store.find_open(phone=phone, action="create")
            if len(opens) != 1:
                return WorkflowResult.needs_human(
                    self.name,
                    ctx.correlation_id,
                    code="pending_ambiguous",
                    message="could not uniquely identify pending create",
                )
            action = store.consume(opens[0].id)

        if action is None:
            return WorkflowResult.failed(
                self.name,
                ctx.correlation_id,
                code="pending_expired_or_replay",
                message="pending confirmation missing, expired, or already used",
            )
        if action.phone != phone:
            return WorkflowResult.failed(
                self.name,
                ctx.correlation_id,
                code="patient_mismatch",
                message="confirmation phone does not match pending action",
            )

        payload = {
            "patient_id": action.patient_id,
            "starts_at": action.payload["starts_at"],
            "status": action.payload.get("status", "booked"),
        }
        try:
            created = ctx.nookal.create_appointment(payload)
        except KillSwitchActive:
            raise
        except NookalError as exc:
            return WorkflowResult.failed(
                self.name,
                ctx.correlation_id,
                code="nookal_write_failed",
                message="failed to create appointment",
                details={"error_type": type(exc).__name__},
            )

        ctx.audit_event(
            "create_appointment.created",
            target_type="appointment",
            target_id=created.appointment_id,
            metadata={"pending_action_id": action.id},
        )
        return WorkflowResult.success(
            self.name,
            ctx.correlation_id,
            data={
                "appointment_id": created.appointment_id,
                "patient_id": created.patient_id,
                "starts_at": created.starts_at.isoformat(),
            },
        )
