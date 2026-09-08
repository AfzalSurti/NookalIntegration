"""
Injected service bundle for workflow runs.

Workflows must receive a WorkflowContext — never reach for get_settings()
singletons, get_queue(), or module-level send() for side effects.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from app.approval import ApprovalQueue
from app.messaging import MessagingService
from app.nookal_client import NookalClient
from app.orchestration.correlation import new_correlation_id
from app.shared.audit import Result, TargetType
from app.shared.clock import Clock, SystemClock


class LLMPort(Protocol):
    """Narrow port so orchestration does not require a concrete LLMService import cycle."""

    def draft(self, template: str, facts: dict[str, Any], **kwargs: Any) -> Any: ...

    def parse_intent(self, message: str) -> Any: ...


AuditFn = Callable[..., Any]


@dataclass
class WorkflowContext:
    nookal: NookalClient
    messaging: MessagingService
    approval: ApprovalQueue
    audit: AuditFn
    clock: Clock = field(default_factory=SystemClock)
    llm: LLMPort | None = None
    pending_actions: Any | None = None  # PendingActionStore when appointment commands need it
    correlation_id: str = field(default_factory=new_correlation_id)
    actor: str = "orchestration"
    # Free-form non-sensitive tags from the caller (scheduler, webhook, dashboard).
    trigger: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def child(self, *, correlation_id: str | None = None, **overrides: Any) -> WorkflowContext:
        """Copy with optional overrides (e.g. new correlation for a sub-step)."""
        values = {
            "nookal": self.nookal,
            "messaging": self.messaging,
            "approval": self.approval,
            "audit": self.audit,
            "clock": self.clock,
            "llm": self.llm,
            "pending_actions": self.pending_actions,
            "correlation_id": correlation_id or self.correlation_id,
            "actor": self.actor,
            "trigger": self.trigger,
            "metadata": dict(self.metadata),
        }
        values.update(overrides)
        return WorkflowContext(**values)

    def audit_event(
        self,
        action: str,
        *,
        target_type: TargetType = "system",
        target_id: str | None = None,
        result: Result = "success",
        metadata: dict[str, Any] | None = None,
    ) -> Any:
        """
        Audit a workflow action. Always attaches correlation_id.
        Caller must not pass patient names / message bodies in metadata.
        """
        meta = {"correlation_id": self.correlation_id}
        if self.trigger:
            meta["trigger"] = self.trigger
        if metadata:
            meta.update(metadata)
        return self.audit(
            actor=self.actor,
            action=action,
            target_type=target_type,
            target_id=target_id or self.correlation_id,
            result=result,
            metadata=meta,
        )
