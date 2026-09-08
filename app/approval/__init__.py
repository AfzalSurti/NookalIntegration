"""
Human approval queue.

LLM (and staff) create Tasks. Only approve() may invoke the registered
downstream handler — that is how draft-only / no-autonomous-send is enforced
in code rather than in a policy doc.
"""
from __future__ import annotations

import json
import threading
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Protocol

from app.shared import audit as audit_mod
from app.shared.clock import Clock, SystemClock
from app.shared.config import get_settings
from app.shared.exceptions import ApprovalError, StateTransitionError


class TaskType(str, Enum):
    LETTER = "letter"
    CERTIFICATE = "certificate"
    WHATSAPP_REPLY = "whatsapp_reply"
    APPOINTMENT_CHANGE = "appointment_change"


class TaskStatus(str, Enum):
    DRAFT = "draft"
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    SENT = "sent"
    FAILED = "failed"


# Legal transitions. Skipping pending_review → approved is impossible by design.
_ALLOWED: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.DRAFT: {TaskStatus.PENDING_REVIEW},
    TaskStatus.PENDING_REVIEW: {TaskStatus.APPROVED, TaskStatus.REJECTED},
    TaskStatus.APPROVED: {TaskStatus.SENT, TaskStatus.FAILED},
    TaskStatus.REJECTED: set(),
    TaskStatus.SENT: set(),
    TaskStatus.FAILED: {TaskStatus.PENDING_REVIEW},  # allow re-queue after fix
}


@dataclass
class Task:
    id: str
    type: TaskType
    status: TaskStatus
    patient_id: str
    created_by: str  # "llm" or staff user id
    content_draft: dict[str, Any]
    reviewed_by: str | None = None
    reviewer_notes: str | None = None
    created_at: str = field(default_factory=lambda: _now())
    updated_at: str = field(default_factory=lambda: _now())

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["type"] = self.type.value
        data["status"] = self.status.value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Task:
        return cls(
            id=data["id"],
            type=TaskType(data["type"]),
            status=TaskStatus(data["status"]),
            patient_id=data["patient_id"],
            created_by=data["created_by"],
            content_draft=data.get("content_draft") or {},
            reviewed_by=data.get("reviewed_by"),
            reviewer_notes=data.get("reviewer_notes"),
            created_at=data["created_at"],
            updated_at=data["updated_at"],
        )


class ApprovalHandler(Protocol):
    def __call__(self, task: Task) -> None: ...


def _now(clock: Clock | None = None) -> str:
    return (clock or SystemClock()).now().isoformat()


class ApprovalQueue:
    def __init__(
        self,
        store_path: Path | None = None,
        *,
        audit: Callable[..., Any] | None = None,
        actor: str = "approval",
        clock: Clock | None = None,
    ) -> None:
        settings = get_settings()
        self._path = store_path or settings.approval.store_path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._audit = audit or audit_mod.log_event
        self._actor = actor
        self._clock: Clock = clock or SystemClock()
        self._lock = threading.RLock()
        self._handlers: dict[TaskType, ApprovalHandler] = {}
        self._tasks: dict[str, Task] = {}
        self._load()

    def register_handler(self, task_type: TaskType, handler: ApprovalHandler) -> None:
        self._handlers[task_type] = handler

    def create_task(
        self,
        *,
        task_type: TaskType,
        patient_id: str,
        created_by: str,
        content_draft: dict[str, Any],
        submit: bool = True,
    ) -> Task:
        """
        Create a task. By default it lands in pending_review immediately —
        drafts that need more editing can pass submit=False.
        """
        stamp = _now(self._clock)
        task = Task(
            id=str(uuid.uuid4()),
            type=task_type,
            status=TaskStatus.DRAFT,
            patient_id=patient_id,
            created_by=created_by,
            content_draft=content_draft,
            created_at=stamp,
            updated_at=stamp,
        )
        with self._lock:
            self._tasks[task.id] = task
            if submit:
                self._transition(task, TaskStatus.PENDING_REVIEW)
            self._persist()
        self._audit(
            actor=self._actor,
            action="create_task",
            target_type="task",
            target_id=task.id,
            result="success",
            metadata={"type": task.type.value, "status": task.status.value},
        )
        return task

    def list_pending(self, role_filter: str | None = None) -> list[Task]:
        """
        role_filter is reserved for dashboard RBAC (e.g. practitioner vs admin).
        Phase 1: all pending tasks returned; filter wiring lands with the UI.
        """
        with self._lock:
            pending = [
                t for t in self._tasks.values() if t.status == TaskStatus.PENDING_REVIEW
            ]
        pending.sort(key=lambda t: t.created_at)
        _ = role_filter  # intentional no-op until roles land
        return pending

    def list_tasks(
        self,
        *,
        status: TaskStatus | None = None,
        task_type: TaskType | None = None,
    ) -> list[Task]:
        """Read-only filtered listing for dashboard views."""
        with self._lock:
            tasks = list(self._tasks.values())
        if status is not None:
            tasks = [t for t in tasks if t.status == status]
        if task_type is not None:
            tasks = [t for t in tasks if t.type == task_type]
        tasks.sort(key=lambda t: t.created_at)
        return tasks

    def get(self, task_id: str) -> Task:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                raise ApprovalError(f"task not found: {task_id}")
            return task

    def approve(
        self,
        task_id: str,
        reviewer_id: str,
        notes: str | None = None,
    ) -> Task:
        """
        Sole entry point for downstream side effects. Handler runs only after
        the task is marked approved; failures move it to failed.
        """
        with self._lock:
            task = self.get(task_id)
            if task.status != TaskStatus.PENDING_REVIEW:
                raise StateTransitionError(
                    f"cannot approve from status={task.status.value}"
                )
            task.reviewed_by = reviewer_id
            task.reviewer_notes = notes
            self._transition(task, TaskStatus.APPROVED)
            self._persist()

        self._audit(
            actor=reviewer_id,
            action="approve_task",
            target_type="task",
            target_id=task.id,
            result="success",
            metadata={"type": task.type.value},
        )

        handler = self._handlers.get(task.type)
        if handler is None:
            with self._lock:
                self._transition(task, TaskStatus.FAILED)
                self._persist()
            self._audit(
                actor=self._actor,
                action="approve_handler_missing",
                target_type="task",
                target_id=task.id,
                result="failure",
                metadata={"type": task.type.value},
            )
            raise ApprovalError(f"no handler registered for type={task.type.value}")

        try:
            handler(task)
        except Exception as exc:
            with self._lock:
                self._transition(task, TaskStatus.FAILED)
                self._persist()
            self._audit(
                actor=self._actor,
                action="approve_handler",
                target_type="task",
                target_id=task.id,
                result="failure",
                metadata={"error_type": type(exc).__name__},
            )
            raise

        with self._lock:
            self._transition(task, TaskStatus.SENT)
            self._persist()
        self._audit(
            actor=self._actor,
            action="task_sent",
            target_type="task",
            target_id=task.id,
            result="success",
            metadata={"type": task.type.value},
        )
        return task

    def reject(
        self,
        task_id: str,
        reviewer_id: str,
        notes: str | None = None,
    ) -> Task:
        with self._lock:
            task = self.get(task_id)
            if task.status != TaskStatus.PENDING_REVIEW:
                raise StateTransitionError(
                    f"cannot reject from status={task.status.value}"
                )
            task.reviewed_by = reviewer_id
            task.reviewer_notes = notes
            self._transition(task, TaskStatus.REJECTED)
            self._persist()
        self._audit(
            actor=reviewer_id,
            action="reject_task",
            target_type="task",
            target_id=task.id,
            result="success",
            metadata={"type": task.type.value},
        )
        return task

    def submit_for_review(self, task_id: str) -> Task:
        with self._lock:
            task = self.get(task_id)
            self._transition(task, TaskStatus.PENDING_REVIEW)
            self._persist()
        return task

    def _transition(self, task: Task, new_status: TaskStatus) -> None:
        allowed = _ALLOWED.get(task.status, set())
        if new_status not in allowed:
            raise StateTransitionError(
                f"illegal transition {task.status.value} → {new_status.value}"
            )
        task.status = new_status
        task.updated_at = _now(self._clock)

    def _load(self) -> None:
        if not self._path.exists():
            return
        with self._path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    task = Task.from_dict(json.loads(line))
                    self._tasks[task.id] = task
                except (json.JSONDecodeError, KeyError, ValueError):
                    continue

    def _persist(self) -> None:
        # Rewrite snapshot — small queue; swap to SQLite when volume warrants it.
        tmp = self._path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            for task in self._tasks.values():
                f.write(json.dumps(task.to_dict(), ensure_ascii=False) + "\n")
        tmp.replace(self._path)


# Convenience module-level API matching the scaffold prompt names.

_queue: ApprovalQueue | None = None


def get_queue() -> ApprovalQueue:
    global _queue
    if _queue is None:
        _queue = ApprovalQueue()
    return _queue


def create_task(**kwargs: Any) -> Task:
    return get_queue().create_task(**kwargs)


def list_pending(role_filter: str | None = None) -> list[Task]:
    return get_queue().list_pending(role_filter)


def approve(task_id: str, reviewer_id: str, notes: str | None = None) -> Task:
    return get_queue().approve(task_id, reviewer_id, notes)


def reject(task_id: str, reviewer_id: str, notes: str | None = None) -> Task:
    return get_queue().reject(task_id, reviewer_id, notes)


def reset_queue_for_tests() -> None:
    global _queue
    _queue = None
