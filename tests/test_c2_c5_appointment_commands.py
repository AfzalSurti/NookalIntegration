from __future__ import annotations

from datetime import datetime, timezone

from app.nookal_client import PatientRef
from app.orchestration.appointment_commands import (
    CancelAppointmentWorkflow,
    CheckAppointmentWorkflow,
    CreateAppointmentWorkflow,
    RescheduleAppointmentWorkflow,
)
from app.orchestration.results import WorkflowStatus
from tests.helpers import WorkflowTestContext
from tests.helpers.fake_llm import FakeIntent, FakeLLM


def test_c2_check_valid(workflow_ctx: WorkflowTestContext) -> None:
    llm = FakeLLM(
        intent=FakeIntent(intent="check_appointment", confidence="high")
    )
    ctx = workflow_ctx.orchestration_context(correlation_id="c2-ok", llm=llm)
    result = CheckAppointmentWorkflow().run(
        ctx, phone="+61411110001", message="when is my appointment?"
    )
    assert result.status == WorkflowStatus.SUCCESS
    assert result.data["patient_id"] == "pat_1001"
    assert any(a["appointment_id"] == "appt_2001" for a in result.data["appointments"])
    assert any(e.action == "check_appointment.lookup" for e in workflow_ctx.audit.events)


def test_c2_unknown_patient(workflow_ctx: WorkflowTestContext) -> None:
    llm = FakeLLM(intent=FakeIntent(intent="check_appointment", confidence="high"))
    ctx = workflow_ctx.orchestration_context(llm=llm)
    result = CheckAppointmentWorkflow().run(
        ctx, phone="+61999999999", message="check please"
    )
    assert result.status == WorkflowStatus.NEEDS_HUMAN
    assert result.error and result.error.code == "patient_not_found"


def test_c2_ambiguous_patient(workflow_ctx: WorkflowTestContext) -> None:
    workflow_ctx.nookal.seed_patient(
        PatientRef(patient_id="pat_dup", phone="+61411110001", display_name="Dup")
    )
    llm = FakeLLM(intent=FakeIntent(intent="check_appointment", confidence="high"))
    ctx = workflow_ctx.orchestration_context(llm=llm)
    result = CheckAppointmentWorkflow().run(
        ctx, phone="+61411110001", message="check"
    )
    assert result.error and result.error.code == "patient_ambiguous"


def test_c2_low_confidence(workflow_ctx: WorkflowTestContext) -> None:
    llm = FakeLLM(
        intent=FakeIntent(intent="check_appointment", confidence="low")
    )
    ctx = workflow_ctx.orchestration_context(llm=llm)
    result = CheckAppointmentWorkflow().run(
        ctx, phone="+61411110001", message="maybe my appt?"
    )
    assert result.status == WorkflowStatus.NEEDS_HUMAN


def test_c2_llm_failure(workflow_ctx: WorkflowTestContext) -> None:
    llm = FakeLLM(fail_parse=True)
    ctx = workflow_ctx.orchestration_context(llm=llm)
    result = CheckAppointmentWorkflow().run(
        ctx, phone="+61411110001", message="check"
    )
    assert result.status == WorkflowStatus.FAILED
    assert result.error and result.error.code == "llm_failure"


def test_c3_reschedule_confirm_yes(workflow_ctx: WorkflowTestContext) -> None:
    new_time = datetime(2026, 9, 10, 11, 0, tzinfo=timezone.utc)
    llm = FakeLLM(
        intent=FakeIntent(
            intent="reschedule_appointment",
            confidence="high",
            extracted_fields={
                "appointment_id": "appt_2001",
                "new_starts_at": new_time.isoformat(),
            },
        )
    )
    ctx = workflow_ctx.orchestration_context(correlation_id="c3-1", llm=llm)
    pending = RescheduleAppointmentWorkflow().run(
        ctx, phone="+61411110001", message="move my appt"
    )
    assert pending.status == WorkflowStatus.NEEDS_CONFIRMATION
    pid = pending.data["pending_action_id"]

    done = RescheduleAppointmentWorkflow().run(
        workflow_ctx.orchestration_context(correlation_id="c3-2", llm=llm),
        phone="+61411110001",
        message="YES",
        confirmation_text="YES",
        pending_action_id=pid,
    )
    assert done.status == WorkflowStatus.SUCCESS
    assert done.data["starts_at"] == new_time.isoformat()

    # Replay must fail
    replay = RescheduleAppointmentWorkflow().run(
        workflow_ctx.orchestration_context(llm=llm),
        phone="+61411110001",
        message="YES",
        confirmation_text="YES",
        pending_action_id=pid,
    )
    assert replay.status == WorkflowStatus.FAILED
    assert replay.error and replay.error.code == "pending_expired_or_replay"


def test_c3_missing_new_time(workflow_ctx: WorkflowTestContext) -> None:
    llm = FakeLLM(
        intent=FakeIntent(
            intent="reschedule_appointment",
            confidence="high",
            extracted_fields={"appointment_id": "appt_2001"},
        )
    )
    result = RescheduleAppointmentWorkflow().run(
        workflow_ctx.orchestration_context(llm=llm),
        phone="+61411110001",
        message="move it",
    )
    assert result.error and result.error.code == "missing_new_time"


def test_c3_kill_switch(workflow_ctx: WorkflowTestContext) -> None:
    new_time = datetime(2026, 9, 10, 11, 0, tzinfo=timezone.utc)
    llm = FakeLLM(
        intent=FakeIntent(
            intent="reschedule_appointment",
            confidence="high",
            extracted_fields={
                "appointment_id": "appt_2001",
                "new_starts_at": new_time.isoformat(),
            },
        )
    )
    pending = RescheduleAppointmentWorkflow().run(
        workflow_ctx.orchestration_context(llm=llm),
        phone="+61411110001",
        message="move",
    )
    workflow_ctx.activate_kill_switch()
    result = RescheduleAppointmentWorkflow().run(
        workflow_ctx.orchestration_context(llm=llm),
        phone="+61411110001",
        message="YES",
        confirmation_text="YES",
        pending_action_id=pending.data["pending_action_id"],
    )
    assert result.status == WorkflowStatus.BLOCKED


def test_c4_cancel_with_confirmation(workflow_ctx: WorkflowTestContext) -> None:
    llm = FakeLLM(
        intent=FakeIntent(
            intent="cancel_appointment",
            confidence="high",
            extracted_fields={"appointment_id": "appt_2001"},
        )
    )
    pending = CancelAppointmentWorkflow().run(
        workflow_ctx.orchestration_context(llm=llm),
        phone="+61411110001",
        message="cancel please",
    )
    assert pending.status == WorkflowStatus.NEEDS_CONFIRMATION
    done = CancelAppointmentWorkflow().run(
        workflow_ctx.orchestration_context(llm=llm),
        phone="+61411110001",
        message="YES",
        confirmation_text="YES",
        pending_action_id=pending.data["pending_action_id"],
    )
    assert done.status == WorkflowStatus.SUCCESS
    assert done.data["status"] == "cancelled"


def test_c4_outside_window(workflow_ctx: WorkflowTestContext) -> None:
    # appt_2001 is tomorrow 10:00; clock is 2026-09-07 08:30 — within 24h? 
    # From 08:30 to next day 10:00 = ~25.5 hours — outside if window is 48? 
    # Use tiny window so tomorrow is inside forbidden zone... 
    # actually outside_window means TOO CLOSE (hours_to_start < window).
    # Tomorrow is ~25.5h away; with window=48 it should fail.
    llm = FakeLLM(
        intent=FakeIntent(
            intent="cancel_appointment",
            confidence="high",
            extracted_fields={"appointment_id": "appt_2001"},
        )
    )
    result = CancelAppointmentWorkflow().run(
        workflow_ctx.orchestration_context(llm=llm),
        phone="+61411110001",
        message="cancel",
        cancellation_window_hours=48,
    )
    assert result.error and result.error.code == "outside_cancellation_window"


def test_c4_already_cancelled(workflow_ctx: WorkflowTestContext) -> None:
    workflow_ctx.nookal.update_appointment("appt_2001", status="cancelled")
    llm = FakeLLM(
        intent=FakeIntent(
            intent="cancel_appointment",
            confidence="high",
            extracted_fields={"appointment_id": "appt_2001"},
        )
    )
    result = CancelAppointmentWorkflow().run(
        workflow_ctx.orchestration_context(llm=llm),
        phone="+61411110001",
        message="cancel",
    )
    assert result.status == WorkflowStatus.SKIPPED


def test_c5_create_with_confirmation(workflow_ctx: WorkflowTestContext) -> None:
    when = datetime(2026, 9, 15, 9, 0, tzinfo=timezone.utc)
    workflow_ctx.nookal.seed_open_slot(when)
    llm = FakeLLM(
        intent=FakeIntent(
            intent="create_appointment",
            confidence="high",
            extracted_fields={"starts_at": when.isoformat()},
        )
    )
    pending = CreateAppointmentWorkflow().run(
        workflow_ctx.orchestration_context(llm=llm),
        phone="+61411110001",
        message="book me in",
    )
    assert pending.status == WorkflowStatus.NEEDS_CONFIRMATION
    done = CreateAppointmentWorkflow().run(
        workflow_ctx.orchestration_context(llm=llm),
        phone="+61411110001",
        message="YES",
        confirmation_text="YES",
        pending_action_id=pending.data["pending_action_id"],
    )
    assert done.status == WorkflowStatus.SUCCESS
    assert "appointment_id" in done.data


def test_c5_missing_fields(workflow_ctx: WorkflowTestContext) -> None:
    llm = FakeLLM(
        intent=FakeIntent(
            intent="create_appointment",
            confidence="high",
            extracted_fields={},
        )
    )
    result = CreateAppointmentWorkflow().run(
        workflow_ctx.orchestration_context(llm=llm),
        phone="+61411110001",
        message="book something",
    )
    assert result.error and result.error.code == "missing_required_fields"
