"""Appointment operational view — mutating actions go through orchestration workflows."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Callable

from app.approval import ApprovalQueue
from app.dashboard.schemas import AppointmentActionRequest, AppointmentSummary
from app.messaging import MessagingService
from app.nookal_client import NookalClient
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
