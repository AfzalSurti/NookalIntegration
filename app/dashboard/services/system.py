"""System status and kill-switch controls — uses existing kill_switch module."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from app.approval import ApprovalQueue
from app.dashboard.schemas import KillSwitchRequest, SystemStatusOut
from app.shared import kill_switch as ks


AuditFn = Callable[..., Any]


class SystemService:
    def __init__(
        self,
        *,
        approval: ApprovalQueue,
        audit: AuditFn,
        kill_switch_path: Path | None,
        environment: str,
        llm_configured: bool,
        nookal_live_configured: bool,
        messaging_live_configured: bool,
    ) -> None:
        self._approval = approval
        self._audit = audit
        self._ks_path = kill_switch_path
        self._environment = environment
        self._llm_configured = llm_configured
        self._nookal_live = nookal_live_configured
        self._messaging_live = messaging_live_configured

    def status(
        self,
        *,
        actor: str,
        role: str,
        correlation_id: str,
    ) -> SystemStatusOut:
        pending = len(self._approval.list_pending())
        active = ks.is_active(self._ks_path)
        out = SystemStatusOut(
            application="ok",
            environment=self._environment,
            kill_switch_active=active,
            llm_configured=self._llm_configured,
            # "configured" means live credentials present — mock still works offline.
            nookal_configured=self._nookal_live,
            messaging_configured=self._messaging_live,
            pending_approvals=pending,
        )
        self._audit(
            actor=actor,
            action="dashboard.system_status",
            target_type="system",
            target_id="status",
            result="success",
            metadata={
                "correlation_id": correlation_id,
                "role": role,
                "kill_switch_active": active,
            },
        )
        return out

    def set_kill_switch(
        self,
        *,
        actor: str,
        role: str,
        correlation_id: str,
        body: KillSwitchRequest,
    ) -> SystemStatusOut:
        if body.active:
            ks.activate(path=self._ks_path, reason=body.reason or f"dashboard:{actor}")
            action = "dashboard.kill_switch_on"
        else:
            ks.deactivate(path=self._ks_path)
            action = "dashboard.kill_switch_off"

        self._audit(
            actor=actor,
            action=action,
            target_type="system",
            target_id="kill_switch",
            result="success",
            metadata={
                "correlation_id": correlation_id,
                "role": role,
                "active": body.active,
            },
        )
        return self.status(actor=actor, role=role, correlation_id=correlation_id)
