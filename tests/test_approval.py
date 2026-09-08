from __future__ import annotations

from pathlib import Path

import pytest

from app.approval import ApprovalQueue, TaskStatus, TaskType
from app.shared.exceptions import StateTransitionError


def test_approve_runs_handler_only_after_review(tmp_path: Path) -> None:
    seen: list[str] = []
    queue = ApprovalQueue(store_path=tmp_path / "tasks.jsonl", audit=lambda **kw: None)

    def handler(task) -> None:
        seen.append(task.id)

    queue.register_handler(TaskType.LETTER, handler)
    task = queue.create_task(
        task_type=TaskType.LETTER,
        patient_id="p1",
        created_by="llm",
        content_draft={"body": "Thanks for the referral."},
    )
    assert task.status == TaskStatus.PENDING_REVIEW
    assert seen == []

    approved = queue.approve(task.id, reviewer_id="dr_het", notes="ok")
    assert approved.status == TaskStatus.SENT
    assert seen == [task.id]


def test_cannot_skip_to_sent(tmp_path: Path) -> None:
    queue = ApprovalQueue(store_path=tmp_path / "tasks.jsonl", audit=lambda **kw: None)
    task = queue.create_task(
        task_type=TaskType.CERTIFICATE,
        patient_id="p1",
        created_by="llm",
        content_draft={},
        submit=False,
    )
    with pytest.raises(StateTransitionError):
        queue._transition(task, TaskStatus.SENT)


def test_reject_does_not_call_handler(tmp_path: Path) -> None:
    called = False

    def handler(task) -> None:
        nonlocal called
        called = True

    queue = ApprovalQueue(store_path=tmp_path / "tasks.jsonl", audit=lambda **kw: None)
    queue.register_handler(TaskType.LETTER, handler)
    task = queue.create_task(
        task_type=TaskType.LETTER,
        patient_id="p1",
        created_by="llm",
        content_draft={},
    )
    queue.reject(task.id, reviewer_id="dr_afzal", notes="rewrite")
    assert not called
    assert queue.get(task.id).status == TaskStatus.REJECTED
