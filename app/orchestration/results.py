"""Workflow result and error conventions."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class WorkflowStatus(str, Enum):
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    BLOCKED = "blocked"  # kill switch or policy halt
    NEEDS_CONFIRMATION = "needs_confirmation"  # patient must confirm
    NEEDS_HUMAN = "needs_human"  # ambiguous / low-confidence / staff
    SKIPPED = "skipped"  # idempotent replay / nothing to do


@dataclass(frozen=True)
class WorkflowErrorInfo:
    """
    Non-sensitive failure descriptor.

    `message` must never contain patient names, clinical content, or message bodies.
    Put identifiers in `details` only (appointment_id, patient_id, …).
    """

    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class WorkflowResult:
    workflow: str
    status: WorkflowStatus
    correlation_id: str
    data: dict[str, Any] = field(default_factory=dict)
    error: WorkflowErrorInfo | None = None

    @property
    def ok(self) -> bool:
        return self.status == WorkflowStatus.SUCCESS

    @property
    def is_terminal_failure(self) -> bool:
        return self.status in {WorkflowStatus.FAILED, WorkflowStatus.BLOCKED}

    @property
    def awaits_input(self) -> bool:
        return self.status in {
            WorkflowStatus.NEEDS_CONFIRMATION,
            WorkflowStatus.NEEDS_HUMAN,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "workflow": self.workflow,
            "status": self.status.value,
            "correlation_id": self.correlation_id,
            "data": self.data,
            "error": self.error.to_dict() if self.error else None,
        }

    @classmethod
    def success(
        cls,
        workflow: str,
        correlation_id: str,
        *,
        data: dict[str, Any] | None = None,
    ) -> WorkflowResult:
        return cls(
            workflow=workflow,
            status=WorkflowStatus.SUCCESS,
            correlation_id=correlation_id,
            data=data or {},
        )

    @classmethod
    def skipped(
        cls,
        workflow: str,
        correlation_id: str,
        *,
        data: dict[str, Any] | None = None,
    ) -> WorkflowResult:
        return cls(
            workflow=workflow,
            status=WorkflowStatus.SKIPPED,
            correlation_id=correlation_id,
            data=data or {},
        )

    @classmethod
    def blocked(
        cls,
        workflow: str,
        correlation_id: str,
        *,
        code: str = "kill_switch",
        message: str = "operation blocked by kill switch",
        details: dict[str, Any] | None = None,
    ) -> WorkflowResult:
        return cls(
            workflow=workflow,
            status=WorkflowStatus.BLOCKED,
            correlation_id=correlation_id,
            error=WorkflowErrorInfo(code=code, message=message, details=details or {}),
        )

    @classmethod
    def failed(
        cls,
        workflow: str,
        correlation_id: str,
        *,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> WorkflowResult:
        return cls(
            workflow=workflow,
            status=WorkflowStatus.FAILED,
            correlation_id=correlation_id,
            data=data or {},
            error=WorkflowErrorInfo(code=code, message=message, details=details or {}),
        )

    @classmethod
    def needs_confirmation(
        cls,
        workflow: str,
        correlation_id: str,
        *,
        data: dict[str, Any] | None = None,
    ) -> WorkflowResult:
        return cls(
            workflow=workflow,
            status=WorkflowStatus.NEEDS_CONFIRMATION,
            correlation_id=correlation_id,
            data=data or {},
        )

    @classmethod
    def needs_human(
        cls,
        workflow: str,
        correlation_id: str,
        *,
        code: str = "needs_human",
        message: str = "routed to staff",
        details: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> WorkflowResult:
        return cls(
            workflow=workflow,
            status=WorkflowStatus.NEEDS_HUMAN,
            correlation_id=correlation_id,
            data=data or {},
            error=WorkflowErrorInfo(code=code, message=message, details=details or {}),
        )
