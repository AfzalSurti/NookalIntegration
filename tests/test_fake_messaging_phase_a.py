from __future__ import annotations

from pathlib import Path

import pytest

from app.messaging import MessagingService, SentLog, TemplateStore
from app.messaging.adapters.fake import FakeAdapter, FakeSendMode, FakeTimeoutError
from app.messaging.adapters.stub import StubAdapter
from app.shared.exceptions import MessagingError


@pytest.fixture()
def fake_whatsapp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[MessagingService, FakeAdapter]:
    flag = tmp_path / "KILL_SWITCH"
    from app.shared import kill_switch as ks

    monkeypatch.setattr(ks, "switch_path", lambda path=None: flag)
    fake = FakeAdapter("whatsapp")
    svc = MessagingService(
        adapters={
            "whatsapp": fake,
            "sms": StubAdapter("sms"),
            "email": StubAdapter("email"),
        },
        templates=TemplateStore(),
        sent_log=SentLog(directory=tmp_path / "sent"),
        audit=lambda *a, **kw: None,
    )
    return svc, fake


def test_fake_success_captures_message(
    fake_whatsapp: tuple[MessagingService, FakeAdapter],
) -> None:
    svc, fake = fake_whatsapp
    msg = svc.send(
        "whatsapp",
        "+61411110001",
        "appointment_reminder",
        {"name": "Alex", "date": "8 Sep", "time": "10:00"},
        patient_id="pat_1001",
        idempotency_key="appt_2001_reminder_2026-09-08",
        caller_role="system",
    )
    assert msg.status == "sent"
    assert len(fake.sent) == 1
    assert fake.sent[0].to == "+61411110001"
    assert "Alex" in fake.sent[0].body
    assert fake.sent[0].mode == FakeSendMode.SUCCESS


def test_fake_failure(
    fake_whatsapp: tuple[MessagingService, FakeAdapter],
) -> None:
    svc, fake = fake_whatsapp
    fake.set_mode(FakeSendMode.FAILURE)
    msg = svc.send(
        "whatsapp",
        "+61411110001",
        "appointment_reminder",
        {"name": "Alex", "date": "8 Sep", "time": "10:00"},
        patient_id="pat_1001",
        idempotency_key="appt_2001_reminder_fail",
        caller_role="system",
    )
    assert msg.status == "failed"
    assert msg.metadata.get("error_type") == "MessagingError"
    assert len(fake.sent) == 1
    assert fake.sent[0].mode == FakeSendMode.FAILURE


def test_fake_timeout(
    fake_whatsapp: tuple[MessagingService, FakeAdapter],
) -> None:
    svc, fake = fake_whatsapp
    fake.set_mode(FakeSendMode.TIMEOUT)
    msg = svc.send(
        "whatsapp",
        "+61411110001",
        "appointment_reminder",
        {"name": "Alex", "date": "8 Sep", "time": "10:00"},
        patient_id="pat_1001",
        idempotency_key="appt_2001_reminder_timeout",
        caller_role="system",
    )
    assert msg.status == "failed"
    assert msg.metadata.get("error_type") == "FakeTimeoutError"
    assert len(fake.sent) == 1


def test_fake_adapter_raises_directly() -> None:
    fake = FakeAdapter("sms", mode=FakeSendMode.TIMEOUT)
    with pytest.raises(FakeTimeoutError):
        fake.send("+61400000000", "hello")
    fake.set_mode(FakeSendMode.FAILURE)
    with pytest.raises(MessagingError):
        fake.send("+61400000000", "hello")
    assert fake.attempt_count == 2
