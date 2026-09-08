"""
Phase C end-to-end workflow coverage — offline only.

Uses MockNookalClient, FakeAdapter, FrozenClock, FakeLLM, synthetic seed.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from app.approval import TaskStatus, TaskType
from app.orchestration.appointment_commands import (
    CancelAppointmentWorkflow,
    CheckAppointmentWorkflow,
    CreateAppointmentWorkflow,
    RescheduleAppointmentWorkflow,
)
from app.orchestration.appointment_reminders import AppointmentRemindersWorkflow
from app.orchestration.certificates import CertificateRequestWorkflow
from app.orchestration.referral_letters import ReferralThankYouWorkflow
from app.orchestration.referrer_sync import ReferrerConflictStore, ReferrerSyncWorkflow
from app.orchestration.results import WorkflowStatus
from app.nookal_client.seed import sync_scenarios
from tests.helpers import WorkflowTestContext
from tests.helpers.fake_llm import FakeDraft, FakeIntent, FakeLLM


def test_e2e_reminder_and_dedupe(workflow_ctx: WorkflowTestContext) -> None:
    r1 = AppointmentRemindersWorkflow().run(
        workflow_ctx.orchestration_context(correlation_id="e2e-rem-1"),
        reminder_date=date(2026, 9, 8),
    )
    r2 = AppointmentRemindersWorkflow().run(
        workflow_ctx.orchestration_context(correlation_id="e2e-rem-2"),
        reminder_date=date(2026, 9, 8),
    )
    assert r1.data["sent"] >= 1
    assert r2.data["skipped"] >= 1
    assert r2.data["sent"] == 0


def test_e2e_check_reschedule_cancel_create(workflow_ctx: WorkflowTestContext) -> None:
    check = CheckAppointmentWorkflow().run(
        workflow_ctx.orchestration_context(
            llm=FakeLLM(intent=FakeIntent("check_appointment", confidence="high"))
        ),
        phone="+61411110001",
        message="when?",
    )
    assert check.status == WorkflowStatus.SUCCESS

    new_time = datetime(2026, 9, 11, 9, 0, tzinfo=timezone.utc)
    pending = RescheduleAppointmentWorkflow().run(
        workflow_ctx.orchestration_context(
            llm=FakeLLM(
                intent=FakeIntent(
                    "reschedule_appointment",
                    {"appointment_id": "appt_2001", "new_starts_at": new_time.isoformat()},
                    "high",
                )
            )
        ),
        phone="+61411110001",
        message="move",
    )
    assert pending.status == WorkflowStatus.NEEDS_CONFIRMATION
    done = RescheduleAppointmentWorkflow().run(
        workflow_ctx.orchestration_context(llm=FakeLLM()),
        phone="+61411110001",
        message="YES",
        confirmation_text="YES",
        pending_action_id=pending.data["pending_action_id"],
    )
    assert done.status == WorkflowStatus.SUCCESS

    # Move clock-relative cancel window: appointment now at Sep 11; clock Sep 7 → ok with 24h
    cancel_p = CancelAppointmentWorkflow().run(
        workflow_ctx.orchestration_context(
            llm=FakeLLM(
                intent=FakeIntent(
                    "cancel_appointment",
                    {"appointment_id": "appt_2001"},
                    "high",
                )
            )
        ),
        phone="+61411110001",
        message="cancel",
    )
    assert cancel_p.status == WorkflowStatus.NEEDS_CONFIRMATION
    cancel_done = CancelAppointmentWorkflow().run(
        workflow_ctx.orchestration_context(llm=FakeLLM()),
        phone="+61411110001",
        confirmation_text="YES",
        pending_action_id=cancel_p.data["pending_action_id"],
        message="YES",
    )
    assert cancel_done.status == WorkflowStatus.SUCCESS

    when = datetime(2026, 9, 20, 10, 0, tzinfo=timezone.utc)
    workflow_ctx.nookal.seed_open_slot(when)
    create_p = CreateAppointmentWorkflow().run(
        workflow_ctx.orchestration_context(
            llm=FakeLLM(
                intent=FakeIntent(
                    "create_appointment",
                    {"starts_at": when.isoformat()},
                    "high",
                )
            )
        ),
        phone="+61411110001",
        message="book",
    )
    assert create_p.status == WorkflowStatus.NEEDS_CONFIRMATION
    create_done = CreateAppointmentWorkflow().run(
        workflow_ctx.orchestration_context(llm=FakeLLM()),
        phone="+61411110001",
        confirmation_text="YES",
        pending_action_id=create_p.data["pending_action_id"],
        message="YES",
    )
    assert create_done.status == WorkflowStatus.SUCCESS


def test_e2e_certificate_and_letter(workflow_ctx: WorkflowTestContext) -> None:
    cert = CertificateRequestWorkflow().run(
        workflow_ctx.orchestration_context(
            correlation_id="e2e-cert",
            llm=FakeLLM(
                intent=FakeIntent(
                    "request_certificate",
                    {"certificate_type": "attendance"},
                    "high",
                )
            ),
        ),
        phone="+61411110001",
        message="cert please",
    )
    assert cert.status == WorkflowStatus.SUCCESS
    task = workflow_ctx.approval.get(cert.data["task_id"])
    assert task.status == TaskStatus.PENDING_REVIEW
    assert task.type == TaskType.CERTIFICATE

    letter = ReferralThankYouWorkflow().run(
        workflow_ctx.orchestration_context(
            correlation_id="e2e-letter",
            llm=FakeLLM(draft=FakeDraft(text="Thanks for the referral.")),
        ),
        referral_id="referral_3001",
    )
    assert letter.status == WorkflowStatus.SUCCESS
    ltask = workflow_ctx.approval.get(letter.data["task_id"])
    assert ltask.status == TaskStatus.PENDING_REVIEW
    assert not any("Thanks for the referral" in str(e.metadata) for e in workflow_ctx.audit.events)


def test_e2e_referrer_sync_and_kill_switch(workflow_ctx: WorkflowTestContext) -> None:
    scenarios = sync_scenarios()
    store = ReferrerConflictStore()
    sync = ReferrerSyncWorkflow().run(
        workflow_ctx.orchestration_context(correlation_id="e2e-sync"),
        candidates=[
            scenarios["exact_match_candidate"],
            scenarios["ambiguous_candidate"],
            scenarios["new_referrer_candidate"],
        ],
        conflict_store=store,
    )
    assert sync.data["exact_updated"] == 1
    assert sync.data["conflict"] == 1
    assert sync.data["new_pending_count"] == 1

    workflow_ctx.activate_kill_switch()
    blocked = AppointmentRemindersWorkflow().run(
        workflow_ctx.orchestration_context(correlation_id="e2e-ks"),
        reminder_date=date(2026, 9, 9),
    )
    assert blocked.data["blocked"] >= 1


def test_e2e_low_confidence_never_writes(workflow_ctx: WorkflowTestContext) -> None:
    before = dict(workflow_ctx.nookal.appointments)
    result = RescheduleAppointmentWorkflow().run(
        workflow_ctx.orchestration_context(
            llm=FakeLLM(
                intent=FakeIntent("reschedule_appointment", confidence="low")
            )
        ),
        phone="+61411110001",
        message="maybe move it",
    )
    assert result.status == WorkflowStatus.NEEDS_HUMAN
    assert workflow_ctx.nookal.appointments == before


def test_e2e_correlation_on_audit(workflow_ctx: WorkflowTestContext) -> None:
    AppointmentRemindersWorkflow().run(
        workflow_ctx.orchestration_context(correlation_id="e2e-corr"),
        reminder_date=date(2026, 9, 8),
    )
    related = [
        e
        for e in workflow_ctx.audit.events
        if e.metadata.get("correlation_id") == "e2e-corr"
    ]
    assert len(related) >= 2
