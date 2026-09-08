from __future__ import annotations

import socket
from pathlib import Path

import pytest

from app.shared.audit import reset_audit_log_for_tests
from app.shared.clock import FrozenClock
from app.shared.config import clear_settings_cache
from app.approval import reset_queue_for_tests
from app.nookal_client.seed import seeded_mock_client
from tests.helpers import (
    CapturingAudit,
    SEED_NOW,
    WorkflowTestContext,
    build_workflow_context,
)


@pytest.fixture(autouse=True)
def _isolate_globals() -> None:
    clear_settings_cache()
    reset_audit_log_for_tests()
    reset_queue_for_tests()
    yield
    clear_settings_cache()
    reset_audit_log_for_tests()
    reset_queue_for_tests()


@pytest.fixture(autouse=True)
def _block_network(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Default suite is offline. Tests marked @pytest.mark.external may opt out."""
    if request.node.get_closest_marker("external"):
        return

    original_connect = socket.socket.connect

    def _guard(self, address):  # type: ignore[no-untyped-def]
        host = address[0] if isinstance(address, tuple) and address else address
        # Allow loopback for ASGI TestClient / socketpair on Windows.
        if host in {"127.0.0.1", "::1", "localhost"}:
            return original_connect(self, address)
        raise RuntimeError("network disabled in default offline tests")

    monkeypatch.setattr(socket.socket, "connect", _guard)


@pytest.fixture()
def frozen_clock() -> FrozenClock:
    return FrozenClock(SEED_NOW)


@pytest.fixture()
def capturing_audit(tmp_path: Path, frozen_clock: FrozenClock) -> CapturingAudit:
    from app.shared.audit import AuditLog

    return CapturingAudit(log=AuditLog(directory=tmp_path / "audit", clock=frozen_clock))


@pytest.fixture()
def seeded_nookal(capturing_audit: CapturingAudit):
    return seeded_mock_client(audit=capturing_audit)


@pytest.fixture()
def fake_messaging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    frozen_clock: FrozenClock,
    capturing_audit: CapturingAudit,
):
    from app.messaging import MessagingService, SentLog, TemplateStore
    from app.messaging.adapters.fake import FakeAdapter
    from app.shared import kill_switch as ks

    flag = tmp_path / "KILL_SWITCH"
    monkeypatch.setattr(ks, "switch_path", lambda path=None: flag)
    fake = FakeAdapter("whatsapp")
    svc = MessagingService(
        adapters={
            "whatsapp": fake,
            "sms": FakeAdapter("sms"),
            "email": FakeAdapter("email"),
        },
        templates=TemplateStore(),
        sent_log=SentLog(directory=tmp_path / "sent", clock=frozen_clock),
        audit=capturing_audit,
        clock=frozen_clock,
    )
    return svc, fake


@pytest.fixture()
def workflow_ctx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> WorkflowTestContext:
    return build_workflow_context(tmp_path, monkeypatch=monkeypatch, seed=True)
