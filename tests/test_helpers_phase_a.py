from __future__ import annotations

from datetime import date, datetime, timezone

from app.approval import TaskType
from tests.helpers import SEED_NOW, WorkflowTestContext


def test_helpers_seeded_nookal(seeded_nookal) -> None:
    assert "pat_1001" in seeded_nookal.patients
    assert seeded_nookal.as_of.isoformat() == "2026-09-07"


def test_helpers_frozen_clock(frozen_clock) -> None:
    assert frozen_clock.now() == SEED_NOW


def test_helpers_fake_messaging(fake_messaging) -> None:
    svc, fake = fake_messaging
    msg = svc.send(
        "whatsapp",
        "+61411110001",
        "appointment_reminder",
        {"name": "Alex", "date": "8 Sep", "time": "10:00"},
        patient_id="pat_1001",
        idempotency_key="helper_msg_1",
        caller_role="system",
    )
    assert msg.status == "sent"
    assert len(fake.sent) == 1


def test_helpers_capturing_audit(capturing_audit, seeded_nookal) -> None:
    seeded_nookal.get_patient("pat_1001")
    assert any(e.action == "get_patient" for e in capturing_audit.events)


def test_workflow_context_bundle(workflow_ctx: WorkflowTestContext) -> None:
    assert workflow_ctx.clock.now() == SEED_NOW
    assert "pat_1001" in workflow_ctx.nookal.patients
    tomorrow = workflow_ctx.nookal.list_appointments(on_date=date(2026, 9, 8))
    assert any(a.appointment_id == "appt_2001" for a in tomorrow)


def test_workflow_ctx_kill_switch_semantics(workflow_ctx: WorkflowTestContext) -> None:
    workflow_ctx.activate_kill_switch()
    workflow_ctx.assert_nookal_write_blocked(
        lambda: workflow_ctx.nookal.create_appointment(
            {
                "patient_id": "pat_1001",
                "starts_at": datetime(2026, 9, 20, 10, 0, tzinfo=timezone.utc),
            }
        )
    )
    blocked = workflow_ctx.messaging.send(
        "whatsapp",
        "+61411110001",
        "appointment_reminder",
        {"name": "Alex", "date": "8 Sep", "time": "10:00"},
        patient_id="pat_1001",
        idempotency_key="helper_blocked_1",
        caller_role="system",
    )
    workflow_ctx.assert_messaging_blocked(blocked)

    # Approval itself does not check kill switch — creating a task still works.
    task = workflow_ctx.approval.create_task(
        task_type=TaskType.LETTER,
        patient_id="pat_1001",
        created_by="llm",
        content_draft={"body": "x"},
    )
    assert task.id
    workflow_ctx.deactivate_kill_switch()


def test_default_suite_stays_offline() -> None:
    """This file is not marked external; network is blocked by conftest."""
    import socket

    with __import__("pytest").raises(RuntimeError, match="network disabled"):
        socket.socket().connect(("93.184.216.34", 80))
