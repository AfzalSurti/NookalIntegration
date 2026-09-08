from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from app.approval import ApprovalQueue, TaskType
from app.messaging import MessagingService, SentLog, TemplateStore
from app.messaging.adapters.fake import FakeAdapter
from app.shared.audit import AuditLog
from app.shared.clock import FrozenClock


FROZEN = datetime(2026, 9, 7, 8, 30, 0, tzinfo=timezone.utc)


def test_frozen_clock_returns_controlled_time() -> None:
    clock = FrozenClock(FROZEN)
    assert clock.now() == FROZEN
    clock.advance(days=1)
    assert clock.now() == datetime(2026, 9, 8, 8, 30, 0, tzinfo=timezone.utc)


def test_audit_uses_injected_clock(tmp_path: Path) -> None:
    clock = FrozenClock(FROZEN)
    log = AuditLog(directory=tmp_path / "audit", clock=clock)
    event = log.log_event("tester", "ping", "system", "1", "success")
    assert event.timestamp == FROZEN.isoformat()
    assert (tmp_path / "audit" / "audit-2026-09-07.jsonl").exists()


def test_approval_uses_injected_clock(tmp_path: Path) -> None:
    clock = FrozenClock(FROZEN)
    queue = ApprovalQueue(
        store_path=tmp_path / "tasks.jsonl",
        audit=lambda *a, **kw: None,
        clock=clock,
    )
    task = queue.create_task(
        task_type=TaskType.LETTER,
        patient_id="pat_1001",
        created_by="llm",
        content_draft={"body": "draft"},
    )
    assert task.created_at == FROZEN.isoformat()
    assert task.updated_at == FROZEN.isoformat()


def test_messaging_uses_injected_clock(tmp_path: Path, monkeypatch) -> None:
    from app.shared import kill_switch as ks

    flag = tmp_path / "KILL_SWITCH"
    monkeypatch.setattr(ks, "switch_path", lambda path=None: flag)

    clock = FrozenClock(FROZEN)
    fake = FakeAdapter("whatsapp")
    svc = MessagingService(
        adapters={"whatsapp": fake, "sms": FakeAdapter("sms"), "email": FakeAdapter("email")},
        templates=TemplateStore(),
        sent_log=SentLog(directory=tmp_path / "sent", clock=clock),
        audit=lambda *a, **kw: None,
        clock=clock,
    )
    msg = svc.send(
        "whatsapp",
        "+61411110001",
        "appointment_reminder",
        {"name": "Alex", "date": "8 Sep", "time": "10:00"},
        patient_id="pat_1001",
        idempotency_key="clock_test_key",
        caller_role="system",
    )
    assert msg.sent_at == FROZEN.isoformat()
