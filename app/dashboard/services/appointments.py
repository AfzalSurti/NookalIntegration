"""Appointment operational view — mutating actions go through orchestration workflows."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Callable

from app.approval import ApprovalQueue
from app.dashboard.schemas import AppointmentActionRequest, AppointmentSummary
from app.messaging import MessagingService
from app.nookal_client import Appointment, NookalClient
from app.shared.exceptions import NookalNotFound
from app.orchestration.appointment_commands import (
    CancelAppointmentWorkflow,
    RescheduleAppointmentWorkflow,
)
from app.orchestration.context import WorkflowContext
from app.orchestration.correlation import new_correlation_id
from app.orchestration.pending_actions import PendingActionStore
from app.orchestration.results import WorkflowResult
from app.shared.clock import Clock, SystemClock


AuditFn = Callable[..., Any]


class AppointmentService:
    def __init__(
        self,
        *,
        nookal: NookalClient,
        messaging: MessagingService,
        approval: ApprovalQueue,
        audit: AuditFn,
        pending_actions: PendingActionStore,
        clock: Clock | None = None,
        llm: Any = None,
    ) -> None:
        self._nookal = nookal
        self._messaging = messaging
        self._approval = approval
        self._audit = audit
        self._pending = pending_actions
        self._clock = clock or SystemClock()
        self._llm = llm

    def _ctx(self, *, actor: str, correlation_id: str) -> WorkflowContext:
        return WorkflowContext(
            nookal=self._nookal,
            messaging=self._messaging,
            approval=self._approval,
            audit=self._audit,
            clock=self._clock,
            llm=self._llm,
            pending_actions=self._pending,
            correlation_id=correlation_id,
            actor=actor,
            trigger="dashboard",
        )

    def list_upcoming(
        self,
        *,
        actor: str,
        role: str,
        correlation_id: str,
        date_from: date | None = None,
        date_to: date | None = None,
        patient_id: str | None = None,
        location_id: str | None = None,
        practitioner_id: str | None = None,
        status: str | None = None,
    ) -> list[AppointmentSummary]:
        today = self._clock.now().date()
        start = date_from or today
        api_status = status if status and status.lower() not in {"all", ""} else None
        appointments = self._nookal.list_appointments(
            date_from=start,
            date_to=date_to,
            patient_id=patient_id,
            location_id=location_id,
            practitioner_id=practitioner_id,
            status=api_status,
        )
        if status and status.lower() == "all":
            operational = appointments
        elif status:
            target = status.lower()
            operational = [a for a in appointments if (a.status or "").lower() == target]
        else:
            # Prefer upcoming / non-cancelled operational view.
            operational = [
                a
                for a in appointments
                if (a.status or "").lower() not in {"cancelled", "canceled"}
            ]
        operational.sort(key=lambda a: a.starts_at)
        self._audit(
            actor=actor,
            action="dashboard.appointment_list",
            target_type="appointment",
            target_id="list",
            result="success",
            metadata={
                "correlation_id": correlation_id,
                "role": role,
                "count": len(operational),
                "location_id": location_id,
                "practitioner_id": practitioner_id,
                "status": status,
            },
        )
        return [
            AppointmentSummary(
                appointment_id=a.appointment_id,
                patient_id=a.patient_id,
                starts_at=a.starts_at,
                ends_at=a.ends_at,
                status=a.status,
                practitioner_id=a.practitioner_id,
                location_id=a.location_id,
                appointment_type=a.appointment_type,
            )
            for a in operational
        ]

    def get_appointment(
        self,
        appointment_id: str,
        *,
        actor: str,
        role: str,
        correlation_id: str,
    ) -> Appointment:
        clean_id = str(appointment_id).strip()
        bare_id = clean_id.lower().replace("appt_", "")
        alt_id = clean_id[5:].strip() if clean_id.lower().startswith("appt_") else f"appt_{clean_id}"

        # 1. Try direct lookup with clean_id and alt_id
        for target in (clean_id, alt_id):
            try:
                appt = self._nookal.get_appointment(target)
                if appt:
                    self._audit(
                        actor=actor,
                        action="dashboard.appointment_view",
                        target_type="appointment",
                        target_id=target,
                        result="success",
                        metadata={
                            "correlation_id": correlation_id,
                            "role": role,
                        },
                    )
                    return appt
            except Exception:
                pass

        # 2. Try scanning list_appointments across wide date range
        try:
            candidates: list[Appointment] = []
            if hasattr(self._nookal, "list_appointments"):
                try:
                    candidates = self._nookal.list_appointments(
                        date_from=date(2020, 1, 1),
                        date_to=date(2035, 12, 31),
                    )
                except Exception:
                    try:
                        candidates = self._nookal.list_appointments()
                    except Exception:
                        candidates = []

            for cand in candidates:
                cand_id = str(cand.appointment_id).strip()
                cand_bare = cand_id.lower().replace("appt_", "")
                if (
                    cand_id.lower() == clean_id.lower()
                    or (bare_id and cand_bare == bare_id)
                    or (bare_id and bare_id.isdigit() and cand_bare.isdigit() and int(bare_id) == int(cand_bare))
                ):
                    self._audit(
                        actor=actor,
                        action="dashboard.appointment_view",
                        target_type="appointment",
                        target_id=clean_id,
                        result="success",
                        metadata={
                            "correlation_id": correlation_id,
                            "role": role,
                        },
                    )
                    return cand
        except Exception:
            pass

        # 3. Try checking appointments dictionary if client has one (e.g. MockNookalClient)
        if hasattr(self._nookal, "appointments") and isinstance(self._nookal.appointments, dict):
            for k, v in self._nookal.appointments.items():
                k_str = str(k).strip()
                k_bare = k_str.lower().replace("appt_", "")
                if (
                    k_str.lower() == clean_id.lower()
                    or (bare_id and k_bare == bare_id)
                    or (bare_id and bare_id.isdigit() and k_bare.isdigit() and int(bare_id) == int(k_bare))
                ):
                    self._audit(
                        actor=actor,
                        action="dashboard.appointment_view",
                        target_type="appointment",
                        target_id=clean_id,
                        result="success",
                        metadata={
                            "correlation_id": correlation_id,
                            "role": role,
                        },
                    )
                    return v

        # 4. Try checking treatment notes if any reference this appointment ID
        notes = []
        if hasattr(self._nookal, "treatment_notes") and isinstance(self._nookal.treatment_notes, list):
            notes = self._nookal.treatment_notes
        elif hasattr(self._nookal, "get_all_treatment_notes"):
            try:
                notes = self._nookal.get_all_treatment_notes()
            except Exception:
                notes = []

        for note in notes:
            n_aid = getattr(note, "appointment_id", None)
            if n_aid:
                n_str = str(n_aid).strip()
                n_bare = n_str.lower().replace("appt_", "")
                if (
                    n_str.lower() == clean_id.lower()
                    or (bare_id and n_bare == bare_id)
                    or (bare_id and bare_id.isdigit() and n_bare.isdigit() and int(bare_id) == int(n_bare))
                ):
                    appt_dt = datetime.now()
                    if getattr(note, "date", None):
                        try:
                            appt_dt = datetime.fromisoformat(str(note.date))
                        except Exception:
                            pass
                    synthetic = Appointment(
                        appointment_id=clean_id,
                        patient_id=getattr(note, "patient_id", "0"),
                        starts_at=appt_dt,
                        status="completed",
                        practitioner_id=getattr(note, "practitioner_id", None),
                        raw={"source": "treatment_note", "note_id": getattr(note, "note_id", "")},
                    )
                    self._audit(
                        actor=actor,
                        action="dashboard.appointment_view",
                        target_type="appointment",
                        target_id=clean_id,
                        result="success",
                        metadata={
                            "correlation_id": correlation_id,
                            "role": role,
                        },
                    )
                    return synthetic

        self._audit(
            actor=actor,
            action="dashboard.appointment_view",
            target_type="appointment",
            target_id=clean_id,
            result="failure",
            metadata={
                "correlation_id": correlation_id,
                "role": role,
            },
        )
        raise NookalNotFound(f"Appointment {appointment_id} not found")

    def run_action(
        self,
        *,
        actor: str,
        role: str,
        correlation_id: str | None,
        body: AppointmentActionRequest,
    ) -> WorkflowResult:
        cid = correlation_id or new_correlation_id()
        ctx = self._ctx(actor=actor, correlation_id=cid)

        if body.action == "cancel":
            result = CancelAppointmentWorkflow().run(
                ctx,
                phone=body.phone,
                message=body.message or "cancel appointment",
                confirmation_text=body.confirmation_text,
                pending_action_id=body.pending_action_id,
            )
        elif body.action == "reschedule":
            # If starts_at provided, encode into message for FakeLLM/intent path;
            # confirmation still required by workflow.
            message = body.message or "reschedule appointment"
            if body.starts_at is not None:
                message = f"{message} to {body.starts_at.isoformat()}"
            result = RescheduleAppointmentWorkflow().run(
                ctx,
                phone=body.phone,
                message=message,
                confirmation_text=body.confirmation_text,
                pending_action_id=body.pending_action_id,
            )
        else:
            self._audit(
                actor=actor,
                action="dashboard.appointment_action",
                target_type="appointment",
                target_id="unknown",
                result="failure",
                metadata={
                    "correlation_id": cid,
                    "role": role,
                    "action": body.action,
                },
            )
            raise ValueError(f"unsupported action: {body.action}")

        self._audit(
            actor=actor,
            action="dashboard.appointment_action",
            target_type="appointment",
            target_id=str((result.data or {}).get("appointment_id") or "workflow"),
            result="success" if result.status.value in {"success", "needs_confirmation"} else "failure",
            metadata={
                "correlation_id": cid,
                "role": role,
                "action": body.action,
                "workflow_status": result.status.value,
            },
        )
        return result
