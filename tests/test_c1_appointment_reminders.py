from __future__ import annotations

from datetime import date

from app.messaging.adapters.fake import FakeSendMode
from app.nookal_client import PatientRef
from app.orchestration.appointment_reminders import (
    AppointmentRemindersWorkflow,
    reminder_idempotency_key,
)
from app.orchestration.results import WorkflowStatus
from tests.helpers import WorkflowTestContext


def test_c1_sends_reminder_for_tomorrow(workflow_ctx: WorkflowTestContext) -> None:
    ctx = workflow_ctx.orchestration_context(correlation_id="c1-ok")
    result = AppointmentRemindersWorkflow().run(ctx)
    assert result.status == WorkflowStatus.SUCCESS
    assert result.data["sent"] >= 1
    assert any(i["appointment_id"] == "appt_2001" for i in result.data["items"] if i["outcome"] == "sent")
    assert len(workflow_ctx.fake_whatsapp.sent) >= 1
    assert any(e.action == "appointment_reminders.sent" for e in workflow_ctx.audit.events)
    assert all(
        e.metadata.get("correlation_id") == "c1-ok"
        for e in workflow_ctx.audit.events
        if e.action.startswith("appointment_reminders")
    )


def test_c1_duplicate_skip(workflow_ctx: WorkflowTestContext) -> None:
    ctx = workflow_ctx.orchestration_context(correlation_id="c1-dup")
    first = AppointmentRemindersWorkflow().run(ctx)
    second = AppointmentRemindersWorkflow().run(
        workflow_ctx.orchestration_context(correlation_id="c1-dup-2")
    )
    assert first.data["sent"] >= 1
    assert second.status in {WorkflowStatus.SKIPPED, WorkflowStatus.SUCCESS}
    assert second.data["skipped"] >= 1
    assert second.data["sent"] == 0


def test_c1_empty_window(workflow_ctx: WorkflowTestContext) -> None:
    ctx = workflow_ctx.orchestration_context(correlation_id="c1-empty")
    result = AppointmentRemindersWorkflow().run(ctx, reminder_date=date(2026, 1, 1))
    assert result.status == WorkflowStatus.SKIPPED
    assert result.data["items"] == []


def test_c1_missing_contact(workflow_ctx: WorkflowTestContext) -> None:
    # Strip phone from tomorrow's patient
    p = workflow_ctx.nookal.get_patient("pat_1001")
    workflow_ctx.nookal.seed_patient(
        PatientRef(
            patient_id=p.patient_id,
            phone=None,
            email=p.email,
            display_name=p.display_name,
            date_of_birth=p.date_of_birth,
            suburb=p.suburb,
            referrer_id=p.referrer_id,
            last_appointment_date=p.last_appointment_date,
        )
    )
    ctx = workflow_ctx.orchestration_context(correlation_id="c1-miss")
    result = AppointmentRemindersWorkflow().run(ctx, reminder_date=date(2026, 9, 8))
    assert result.status in {WorkflowStatus.FAILED, WorkflowStatus.PARTIAL}
    item = next(i for i in result.data["items"] if i["appointment_id"] == "appt_2001")
    assert item["outcome"] == "failed"
    assert item["code"] == "missing_contact"


def test_c1_send_failure(workflow_ctx: WorkflowTestContext) -> None:
    workflow_ctx.fake_whatsapp.set_mode(FakeSendMode.FAILURE)
    ctx = workflow_ctx.orchestration_context(correlation_id="c1-fail")
    result = AppointmentRemindersWorkflow().run(ctx, reminder_date=date(2026, 9, 8))
    assert result.data["failed"] >= 1
    assert result.status in {WorkflowStatus.FAILED, WorkflowStatus.PARTIAL}


def test_c1_kill_switch_blocks(workflow_ctx: WorkflowTestContext) -> None:
    workflow_ctx.activate_kill_switch()
    ctx = workflow_ctx.orchestration_context(correlation_id="c1-ks")
    result = AppointmentRemindersWorkflow().run(ctx, reminder_date=date(2026, 9, 8))
    assert result.data["blocked"] >= 1
    assert result.status in {WorkflowStatus.BLOCKED, WorkflowStatus.PARTIAL}
    assert workflow_ctx.fake_whatsapp.sent == []


def test_c1_idempotency_key_format() -> None:
    assert reminder_idempotency_key("appt_2001", date(2026, 9, 8)) == (
        "appt_2001_reminder_2026-09-08"
    )
