from __future__ import annotations

import ast
from typing import Any

from app.orchestration import (
    BaseWorkflow,
    WorkflowContext,
    WorkflowExecutionError,
    WorkflowResult,
    WorkflowStatus,
    new_correlation_id,
)
from app.shared.exceptions import KillSwitchActive, repo_root
from tests.helpers import WorkflowTestContext


class _OkWorkflow(BaseWorkflow):
    name = "phase_b_ok"

    def execute(self, ctx: WorkflowContext, **params: Any) -> WorkflowResult:
        ctx.audit_event("phase_b_ok.step", metadata={"marker": "inside"})
        return WorkflowResult.success(
            self.name, ctx.correlation_id, data={"echo": params.get("value")}
        )


class _FailWorkflow(BaseWorkflow):
    name = "phase_b_fail"

    def execute(self, ctx: WorkflowContext, **params: Any) -> WorkflowResult:
        raise WorkflowExecutionError("demo_fail", "controlled failure", details={"x": 1})


class _BoomWorkflow(BaseWorkflow):
    name = "phase_b_boom"

    def execute(self, ctx: WorkflowContext, **params: Any) -> WorkflowResult:
        raise RuntimeError("secret patient detail must not appear in audit")


class _KillSwitchWorkflow(BaseWorkflow):
    name = "phase_b_ks"

    def execute(self, ctx: WorkflowContext, **params: Any) -> WorkflowResult:
        raise KillSwitchActive("nookal.create_appointment")


def test_correlation_ids_are_unique_uuid_strings() -> None:
    a = new_correlation_id()
    b = new_correlation_id()
    assert a != b
    assert len(a) == 36
    assert a.count("-") == 4


def test_workflow_context_is_injected(workflow_ctx: WorkflowTestContext) -> None:
    ctx = workflow_ctx.orchestration_context(correlation_id="corr-fixed")
    assert ctx.nookal is workflow_ctx.nookal
    assert ctx.messaging is workflow_ctx.messaging
    assert ctx.approval is workflow_ctx.approval
    assert ctx.correlation_id == "corr-fixed"
    assert ctx.llm is None


def test_base_workflow_audits_success(workflow_ctx: WorkflowTestContext) -> None:
    ctx = workflow_ctx.orchestration_context(correlation_id="corr-ok")
    result = _OkWorkflow().run(ctx, value=42)
    assert result.status == WorkflowStatus.SUCCESS
    assert result.ok
    assert result.data["echo"] == 42
    actions = [e.action for e in workflow_ctx.audit.events]
    assert "phase_b_ok.started" in actions
    assert "phase_b_ok.step" in actions
    assert "phase_b_ok.finished" in actions
    started = next(e for e in workflow_ctx.audit.events if e.action == "phase_b_ok.started")
    assert started.metadata["correlation_id"] == "corr-ok"


def test_base_workflow_audits_controlled_failure(workflow_ctx: WorkflowTestContext) -> None:
    ctx = workflow_ctx.orchestration_context(correlation_id="corr-fail")
    result = _FailWorkflow().run(ctx)
    assert result.status == WorkflowStatus.FAILED
    assert result.error and result.error.code == "demo_fail"
    actions = [e.action for e in workflow_ctx.audit.events]
    assert "phase_b_fail.failed" in actions


def test_unhandled_error_does_not_leak_message(workflow_ctx: WorkflowTestContext) -> None:
    ctx = workflow_ctx.orchestration_context(correlation_id="corr-boom")
    result = _BoomWorkflow().run(ctx)
    assert result.status == WorkflowStatus.FAILED
    assert result.error and result.error.code == "unhandled"
    assert "patient" not in result.error.message.lower()
    payload = " ".join(str(e.metadata) for e in workflow_ctx.audit.events)
    assert "secret patient detail" not in payload


def test_kill_switch_maps_to_blocked(workflow_ctx: WorkflowTestContext) -> None:
    ctx = workflow_ctx.orchestration_context(correlation_id="corr-ks")
    result = _KillSwitchWorkflow().run(ctx)
    assert result.status == WorkflowStatus.BLOCKED
    assert result.is_terminal_failure
    assert result.error and result.error.code == "kill_switch"


def test_result_conventions() -> None:
    cid = "c1"
    assert WorkflowResult.skipped("w", cid).status == WorkflowStatus.SKIPPED
    assert WorkflowResult.needs_confirmation("w", cid, data={"appt": "1"}).awaits_input
    assert WorkflowResult.needs_human("w", cid).awaits_input
    assert not WorkflowResult.failed("w", cid, code="x", message="y").ok


def test_orchestration_has_no_openclaw_imports() -> None:
    root = repo_root() / "app" / "orchestration"
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert "openclaw" not in alias.name.lower()
            elif isinstance(node, ast.ImportFrom):
                mod = (node.module or "").lower()
                assert "openclaw" not in mod


def test_child_context_preserves_services(workflow_ctx: WorkflowTestContext) -> None:
    parent = workflow_ctx.orchestration_context(correlation_id="parent")
    child = parent.child(correlation_id="child", trigger="webhook")
    assert child.nookal is parent.nookal
    assert child.correlation_id == "child"
    assert child.trigger == "webhook"
