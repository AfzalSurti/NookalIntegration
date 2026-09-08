from __future__ import annotations

from pathlib import Path

import pytest

from app.messaging import MessagingService, SentLog, TemplateStore
from app.messaging.adapters.stub import StubAdapter
from app.shared.exceptions import PermissionDenied
from app.shared.kill_switch import activate, deactivate


@pytest.fixture()
def messaging(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[MessagingService, StubAdapter]:
    flag = tmp_path / "KILL_SWITCH"
    from app.shared import kill_switch as ks

    monkeypatch.setattr(ks, "switch_path", lambda path=None: flag)

    stub = StubAdapter("whatsapp")
    svc = MessagingService(
        adapters={"whatsapp": stub, "sms": StubAdapter("sms"), "email": StubAdapter("email")},
        templates=TemplateStore(),
        sent_log=SentLog(directory=tmp_path / "sent"),
        audit=lambda **kw: None,
    )
    return svc, stub


def test_send_reminder(messaging: tuple[MessagingService, StubAdapter]) -> None:
    svc, stub = messaging
    msg = svc.send(
        "whatsapp",
        "+61400000000",
        "appointment_reminder",
        {"name": "Jane", "date": "10 Sep", "time": "10:00"},
        patient_id="p1",
        idempotency_key="appt_1_reminder_2026-09-10",
        caller_role="system",
    )
    assert msg.status == "sent"
    assert len(stub.sent) == 1
    assert "Jane" in stub.sent[0]["body"]


def test_idempotency(messaging: tuple[MessagingService, StubAdapter]) -> None:
    svc, stub = messaging
    key = "appt_1_reminder_2026-09-10"
    kwargs = dict(
        channel="whatsapp",
        patient_contact="+61400000000",
        template_id="appointment_reminder",
        context={"name": "Jane", "date": "10 Sep", "time": "10:00"},
        patient_id="p1",
        idempotency_key=key,
        caller_role="system",
    )
    assert svc.send(**kwargs).status == "sent"
    assert svc.send(**kwargs).status == "skipped"
    assert len(stub.sent) == 1


def test_role_blocks_marketing(messaging: tuple[MessagingService, StubAdapter]) -> None:
    svc, _ = messaging
    with pytest.raises(PermissionDenied):
        svc.send(
            "whatsapp",
            "+61400000000",
            "appointment_reminder",
            {"name": "Jane", "date": "10 Sep", "time": "10:00"},
            patient_id="p1",
            idempotency_key="mkt_1",
            caller_role="staff",
            send_class="marketing",
        )


def test_kill_switch(messaging: tuple[MessagingService, StubAdapter], tmp_path: Path) -> None:
    svc, stub = messaging
    flag = tmp_path / "KILL_SWITCH"
    activate(path=flag)
    try:
        msg = svc.send(
            "whatsapp",
            "+61400000000",
            "appointment_reminder",
            {"name": "Jane", "date": "10 Sep", "time": "10:00"},
            patient_id="p1",
            idempotency_key="appt_2_reminder_2026-09-11",
            caller_role="system",
        )
        assert msg.status == "blocked"
        assert stub.sent == []
    finally:
        deactivate(path=flag)
