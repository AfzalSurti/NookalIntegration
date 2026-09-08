"""
Reusable workflow test harness for Phase B+.

Documents known kill-switch semantics (do not "fix" them here):
- Nookal writes RAISE KillSwitchActive
- Messaging returns OutboundMessage(status="blocked") without raising
- ApprovalQueue itself does NOT check the kill switch; handlers do

Referrer audit target_type remains "patient_record" until a later schema pass.
HttpNookalClient.save_document stays NotImplemented until official docs.
messaging.max_retries is unused by MessagingService; retries belong in workflows.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from app.approval import ApprovalQueue
from app.messaging import MessagingService, SentLog, TemplateStore
from app.messaging.adapters.fake import FakeAdapter
from app.nookal_client import MockNookalClient
from app.nookal_client.seed import seeded_mock_client
from app.orchestration.context import WorkflowContext
from app.orchestration.correlation import new_correlation_id
from app.orchestration.pending_actions import PendingActionStore
from app.shared.audit import AuditEvent, AuditLog
from app.shared.clock import FrozenClock
from app.shared.exceptions import KillSwitchActive
from app.shared import kill_switch as kill_switch_mod

SEED_NOW = datetime(2026, 9, 7, 8, 30, 0, tzinfo=timezone.utc)


@dataclass
class CapturingAudit:
    """In-memory audit sink that still enforces scrubbing via AuditLog when desired."""

    events: list[AuditEvent] = field(default_factory=list)
    log: AuditLog | None = None

    def __call__(self, *args: Any, **kwargs: Any) -> AuditEvent | None:
        # Support positional (mock nookal) and keyword (messaging/approval) styles,
        # including mixed: positional args + metadata= keyword.
        if "actor" in kwargs:
            actor = kwargs["actor"]
            action = kwargs["action"]
            target_type = kwargs["target_type"]
            target_id = kwargs["target_id"]
            result = kwargs["result"]
            metadata = kwargs.get("metadata")
        else:
            actor, action, target_type, target_id, result = args[:5]
            metadata = kwargs.get("metadata")
            if metadata is None and len(args) >= 6 and isinstance(args[5], dict):
                metadata = args[5]

        if self.log is not None:
            event = self.log.log_event(
                actor=actor,
                action=action,
                target_type=target_type,
                target_id=target_id,
                result=result,
                metadata=metadata if isinstance(metadata, dict) else None,
            )
        else:
            event = AuditEvent(
                timestamp=SEED_NOW.isoformat(),
                actor=actor,
                action=action,
                target_type=target_type,
                target_id=str(target_id),
                result=result,
                metadata=metadata if isinstance(metadata, dict) else {},
            )
        self.events.append(event)
        return event


@dataclass
class WorkflowTestContext:
    """Bundled fakes for writing workflow tests without rebuilding infrastructure."""

    clock: FrozenClock
    nookal: MockNookalClient
    messaging: MessagingService
    fake_whatsapp: FakeAdapter
    approval: ApprovalQueue
    audit: CapturingAudit
    tmp_path: Path
    kill_switch_path: Path
    pending_actions: PendingActionStore

    def activate_kill_switch(self, reason: str = "test") -> None:
        kill_switch_mod.activate(path=self.kill_switch_path, reason=reason)

    def deactivate_kill_switch(self) -> None:
        kill_switch_mod.deactivate(path=self.kill_switch_path)

    def assert_nookal_write_blocked(self, fn) -> None:
        """Nookal kill-switch semantics: must RAISE."""
        with pytest.raises(KillSwitchActive):
            fn()

    def assert_messaging_blocked(self, result) -> None:
        """Messaging kill-switch semantics: status blocked, no raise."""
        assert getattr(result, "status", None) == "blocked"

    def orchestration_context(
        self,
        *,
        correlation_id: str | None = None,
        trigger: str | None = "test",
        llm: Any = None,
    ) -> WorkflowContext:
        """Build a production WorkflowContext over these test doubles."""
        return WorkflowContext(
            nookal=self.nookal,
            messaging=self.messaging,
            approval=self.approval,
            audit=self.audit,
            clock=self.clock,
            llm=llm,
            pending_actions=self.pending_actions,
            correlation_id=correlation_id or new_correlation_id(),
            actor="orchestration.test",
            trigger=trigger,
        )


def build_workflow_context(
    tmp_path: Path,
    *,
    monkeypatch: pytest.MonkeyPatch | None = None,
    seed: bool = True,
) -> WorkflowTestContext:
    clock = FrozenClock(SEED_NOW)
    kill_flag = tmp_path / "KILL_SWITCH"
    if monkeypatch is not None:
        monkeypatch.setattr(kill_switch_mod, "switch_path", lambda path=None: kill_flag)

    audit_dir = tmp_path / "audit"
    audit_log = AuditLog(directory=audit_dir, clock=clock)
    capturing = CapturingAudit(log=audit_log)

    if seed:
        nookal = seeded_mock_client(audit=capturing)
    else:
        nookal = MockNookalClient(audit=capturing, as_of=SEED_NOW.date())

    fake = FakeAdapter("whatsapp")
    messaging = MessagingService(
        adapters={
            "whatsapp": fake,
            "sms": FakeAdapter("sms"),
            "email": FakeAdapter("email"),
        },
        templates=TemplateStore(),
        sent_log=SentLog(directory=tmp_path / "sent", clock=clock),
        audit=capturing,
        clock=clock,
    )
    approval = ApprovalQueue(
        store_path=tmp_path / "tasks.jsonl",
        audit=capturing,
        clock=clock,
    )
    pending = PendingActionStore(clock=clock, ttl_minutes=30)
    return WorkflowTestContext(
        clock=clock,
        nookal=nookal,
        messaging=messaging,
        fake_whatsapp=fake,
        approval=approval,
        audit=capturing,
        tmp_path=tmp_path,
        kill_switch_path=kill_flag,
        pending_actions=pending,
    )
