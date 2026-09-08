from __future__ import annotations

from datetime import date

from app.messaging.adapters.fake import FakeSendMode
from app.orchestration.appointment_reminders import AppointmentRemindersWorkflow
from app.orchestration.results import WorkflowStatus
from app.orchestration.retry import RetryPolicy, call_with_retries, classify_batch_status, is_transient
from app.shared.exceptions import KillSwitchActive, NookalServerError
from tests.helpers import WorkflowTestContext


def test_c9_classify_batch_status() -> None:
    assert classify_batch_status(sent=1, skipped=0, failed=0, blocked=0) == WorkflowStatus.SUCCESS
    assert classify_batch_status(sent=1, skipped=0, failed=1, blocked=0) == WorkflowStatus.PARTIAL
    assert classify_batch_status(sent=0, skipped=0, failed=2, blocked=0) == WorkflowStatus.FAILED
    assert classify_batch_status(sent=0, skipped=0, failed=0, blocked=2) == WorkflowStatus.BLOCKED
    assert classify_batch_status(sent=0, skipped=3, failed=0, blocked=0) == WorkflowStatus.SKIPPED


def test_c9_retry_then_success(workflow_ctx: WorkflowTestContext) -> None:
    fake = workflow_ctx.fake_whatsapp
    attempts = {"n": 0}

    original = fake.send

    def flaky(to, body, *, metadata=None):
        attempts["n"] += 1
        if attempts["n"] < 2:
            fake.mode = FakeSendMode.TIMEOUT
        else:
            fake.mode = FakeSendMode.SUCCESS
        return original(to, body, metadata=metadata)

    fake.send = flaky  # type: ignore[method-assign]
    result = AppointmentRemindersWorkflow().run(
        workflow_ctx.orchestration_context(correlation_id="c9-retry"),
        reminder_date=date(2026, 9, 8),
    )
    assert result.data["sent"] >= 1
    assert attempts["n"] >= 2
    assert any(e.action == "appointment_reminders.retry" for e in workflow_ctx.audit.events)


def test_c9_kill_switch_not_retried(workflow_ctx: WorkflowTestContext) -> None:
    workflow_ctx.activate_kill_switch()
    result = AppointmentRemindersWorkflow().run(
        workflow_ctx.orchestration_context(correlation_id="c9-ks"),
        reminder_date=date(2026, 9, 8),
    )
    assert result.data["blocked"] >= 1
    assert not any(e.action == "appointment_reminders.retry" for e in workflow_ctx.audit.events)


def test_c9_call_with_retries_raises_permanent() -> None:
    calls = {"n": 0}

    def boom():
        calls["n"] += 1
        raise KillSwitchActive("x")

    try:
        call_with_retries(boom, policy=RetryPolicy(max_attempts=3))
        assert False, "expected raise"
    except KillSwitchActive:
        pass
    assert calls["n"] == 1


def test_c9_call_with_retries_transient() -> None:
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise NookalServerError("temp")
        return "ok"

    assert call_with_retries(flaky, policy=RetryPolicy(max_attempts=3)) == "ok"
    assert calls["n"] == 3
    assert is_transient(NookalServerError("x"))
