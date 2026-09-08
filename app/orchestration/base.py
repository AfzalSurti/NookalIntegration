"""
Base workflow runner.

Subclasses implement execute(); run() adds correlation-scoped audit and
maps known failures to WorkflowResult without leaking sensitive detail.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar

from app.orchestration.context import WorkflowContext
from app.orchestration.errors import WorkflowExecutionError
from app.orchestration.results import WorkflowResult, WorkflowStatus
from app.shared.exceptions import KillSwitchActive


class BaseWorkflow(ABC):
    """Deterministic application workflow. No OpenClaw / channel SDK code here."""

    name: ClassVar[str] = "unnamed"

    def run(self, ctx: WorkflowContext, **params: Any) -> WorkflowResult:
        self._audit_start(ctx, params)
        try:
            result = self.execute(ctx, **params)
        except KillSwitchActive as exc:
            ctx.audit_event(
                f"{self.name}.blocked",
                result="blocked",
                metadata={"operation": exc.operation, "error_type": "KillSwitchActive"},
            )
            return WorkflowResult.blocked(
                self.name,
                ctx.correlation_id,
                details={"operation": exc.operation},
            )
        except WorkflowExecutionError as exc:
            ctx.audit_event(
                f"{self.name}.failed",
                result="failure",
                metadata={"code": exc.code, "error_type": type(exc).__name__},
            )
            return WorkflowResult.failed(
                self.name,
                ctx.correlation_id,
                code=exc.code,
                message=str(exc),
                details=exc.details,
            )
        except Exception as exc:
            # Unexpected errors — log type only, never assume exc args are safe.
            ctx.audit_event(
                f"{self.name}.failed",
                result="failure",
                metadata={"code": "unhandled", "error_type": type(exc).__name__},
            )
            return WorkflowResult.failed(
                self.name,
                ctx.correlation_id,
                code="unhandled",
                message="workflow raised an unexpected error",
                details={"error_type": type(exc).__name__},
            )

        if not isinstance(result, WorkflowResult):
            ctx.audit_event(
                f"{self.name}.failed",
                result="failure",
                metadata={"code": "invalid_result"},
            )
            return WorkflowResult.failed(
                self.name,
                ctx.correlation_id,
                code="invalid_result",
                message="workflow returned a non-WorkflowResult value",
            )

        # Ensure correlation/workflow name are consistent even if subclass forgot.
        if result.correlation_id != ctx.correlation_id or result.workflow != self.name:
            result = WorkflowResult(
                workflow=self.name,
                status=result.status,
                correlation_id=ctx.correlation_id,
                data=result.data,
                error=result.error,
            )

        self._audit_finish(ctx, result)
        return result

    @abstractmethod
    def execute(self, ctx: WorkflowContext, **params: Any) -> WorkflowResult:
        """Implement clinic logic here. Use ctx.nookal / messaging / approval / llm only."""

    def _audit_start(self, ctx: WorkflowContext, params: dict[str, Any]) -> None:
        safe_keys = sorted(k for k in params.keys() if not k.startswith("_"))
        ctx.audit_event(
            f"{self.name}.started",
            metadata={"param_keys": safe_keys},
        )

    def _audit_finish(self, ctx: WorkflowContext, result: WorkflowResult) -> None:
        outcome: str
        if result.status == WorkflowStatus.BLOCKED:
            outcome = "blocked"
        elif result.status in {WorkflowStatus.FAILED}:
            outcome = "failure"
        else:
            outcome = "success"
        meta: dict[str, Any] = {"status": result.status.value}
        if result.error:
            meta["code"] = result.error.code
        audit_result = "blocked" if outcome == "blocked" else (
            "failure" if outcome == "failure" else "success"
        )
        ctx.audit_event(f"{self.name}.finished", result=audit_result, metadata=meta)
