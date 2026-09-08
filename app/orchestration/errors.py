"""Orchestration-local errors (not D1–D5 surface)."""
from __future__ import annotations

from typing import Any

from app.shared.exceptions import AutomationError


class WorkflowExecutionError(AutomationError):
    """Raised by a workflow body when it wants BaseWorkflow to map to FAILED."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.details = details or {}
        super().__init__(message)
