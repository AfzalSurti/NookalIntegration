"""Approval queue application service — always delegates to ApprovalQueue."""
from __future__ import annotations

from typing import Any, Callable

from app.approval import ApprovalQueue, Task, TaskStatus, TaskType
from app.dashboard.schemas import ReviewActionRequest, SafeDraftOut, TaskOut
from app.shared.exceptions import ApprovalError, StateTransitionError


AuditFn = Callable[..., Any]


def safe_draft_from_task(task: Task) -> SafeDraftOut:
    draft = task.content_draft or {}
    return SafeDraftOut(
        template_id=str(draft["template_id"]) if draft.get("template_id") else None,
        letter_type=str(draft["letter_type"]) if draft.get("letter_type") else None,
        certificate_type=str(draft["certificate_type"]) if draft.get("certificate_type") else None,
        status_tag=str(draft["status_tag"]) if draft.get("status_tag") else None,
        source=str(draft["source"]) if draft.get("source") else None,
        keys=sorted(k for k in draft.keys() if k != "source_facts"),
    )


def task_to_out(task: Task) -> TaskOut:
    return TaskOut(
        id=task.id,
        type=task.type.value,
        status=task.status.value,
        patient_id=task.patient_id,
        created_by=task.created_by,
        reviewed_by=task.reviewed_by,
        reviewer_notes=task.reviewer_notes,
        created_at=task.created_at,
        updated_at=task.updated_at,
        safe_draft=safe_draft_from_task(task),
    )


class ApprovalService:
    """
    Thin adapter over ApprovalQueue.

    Dashboard MUST NOT mutate task status or invoke document/messaging handlers
    except via ApprovalQueue.approve / reject.
    """

    def __init__(self, *, approval: ApprovalQueue, audit: AuditFn) -> None:
        self._approval = approval
        self._audit = audit

    @property
    def queue(self) -> ApprovalQueue:
        return self._approval

    def list_tasks(
        self,
        *,
        actor: str,
        role: str,
        correlation_id: str,
        status: str | None = None,
        task_type: str | None = None,
        pending_only: bool = True,
        audit_list: bool = True,
    ) -> list[TaskOut]:
        status_enum = TaskStatus(status) if status else (TaskStatus.PENDING_REVIEW if pending_only else None)
        type_enum = TaskType(task_type) if task_type else None
        if pending_only and status is None and task_type is None:
            tasks = self._approval.list_pending(role_filter=role)
        else:
            tasks = self._approval.list_tasks(status=status_enum, task_type=type_enum)
        if audit_list:
            self._audit(
                actor=actor,
                action="dashboard.approval_list",
                target_type="task",
                target_id="list",
                result="success",
                metadata={
                    "correlation_id": correlation_id,
                    "role": role,
                    "count": len(tasks),
                    "pending_only": pending_only,
                },
            )
        return [task_to_out(t) for t in tasks]

    def get_task(self, task_id: str) -> TaskOut:
        return task_to_out(self._approval.get(task_id))

    def approve(
        self,
        task_id: str,
        *,
        reviewer_id: str,
        correlation_id: str,
        body: ReviewActionRequest | None = None,
    ) -> TaskOut:
        notes = body.notes if body else None
        try:
            task = self._approval.approve(task_id, reviewer_id, notes=notes)
        except (StateTransitionError, ApprovalError):
            self._audit(
                actor=reviewer_id,
                action="dashboard.approval_approve",
                target_type="task",
                target_id=task_id,
                result="failure",
                metadata={"correlation_id": correlation_id, "reason": "rejected"},
            )
            raise
        self._audit(
            actor=reviewer_id,
            action="dashboard.approval_approve",
            target_type="task",
            target_id=task.id,
            result="success",
            metadata={
                "correlation_id": correlation_id,
                "type": task.type.value,
                "status": task.status.value,
            },
        )
        return task_to_out(task)

    def reject(
        self,
        task_id: str,
        *,
        reviewer_id: str,
        correlation_id: str,
        body: ReviewActionRequest | None = None,
    ) -> TaskOut:
        notes = body.notes if body else None
        try:
            task = self._approval.reject(task_id, reviewer_id, notes=notes)
        except StateTransitionError:
            self._audit(
                actor=reviewer_id,
                action="dashboard.approval_reject",
                target_type="task",
                target_id=task_id,
                result="failure",
                metadata={"correlation_id": correlation_id, "reason": "state_transition"},
            )
            raise
        self._audit(
            actor=reviewer_id,
            action="dashboard.approval_reject",
            target_type="task",
            target_id=task.id,
            result="success",
            metadata={
                "correlation_id": correlation_id,
                "type": task.type.value,
                "status": task.status.value,
            },
        )
        return task_to_out(task)

    def document_tasks(
        self,
        *,
        actor: str,
        role: str,
        correlation_id: str,
    ) -> list[TaskOut]:
        letters = self.list_tasks(
            actor=actor,
            role=role,
            correlation_id=correlation_id,
            task_type=TaskType.LETTER.value,
            pending_only=True,
            audit_list=False,
        )
        certs = self.list_tasks(
            actor=actor,
            role=role,
            correlation_id=correlation_id,
            task_type=TaskType.CERTIFICATE.value,
            pending_only=True,
            audit_list=False,
        )
        seen = {t.id for t in letters}
        merged = list(letters) + [t for t in certs if t.id not in seen]
        merged.sort(key=lambda t: t.created_at)
        self._audit(
            actor=actor,
            action="dashboard.document_list",
            target_type="document",
            target_id="list",
            result="success",
            metadata={
                "correlation_id": correlation_id,
                "role": role,
                "count": len(merged),
            },
        )
        return merged
