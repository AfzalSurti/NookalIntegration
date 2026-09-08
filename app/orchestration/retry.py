"""
Workflow-level retry helpers for transient failures.

Does not invent provider-specific backoff beyond simple capped attempts.
Permanent validation / needs_human / blocked outcomes must not be retried.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, TypeVar

from app.messaging.adapters.fake import FakeTimeoutError
from app.orchestration.results import WorkflowStatus
from app.shared.exceptions import (
    KillSwitchActive,
    MessagingError,
    NookalRateLimit,
    NookalServerError,
)

T = TypeVar("T")

# Error types treated as transient at the workflow layer.
TRANSIENT_EXCEPTIONS = (
    NookalServerError,
    NookalRateLimit,
    FakeTimeoutError,
)


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            object.__setattr__(self, "max_attempts", 1)


def is_transient(exc: BaseException) -> bool:
    if isinstance(exc, KillSwitchActive):
        return False
    if isinstance(exc, TRANSIENT_EXCEPTIONS):
        return True
    # Generic messaging failure may be transient; FakeTimeoutError already covered.
    if isinstance(exc, MessagingError) and "timeout" in str(exc).lower():
        return True
    return False


def call_with_retries(
    fn: Callable[[], T],
    *,
    policy: RetryPolicy | None = None,
    on_retry: Callable[[int, BaseException], None] | None = None,
) -> T:
    """
    Execute fn up to policy.max_attempts while errors are transient.
    Non-transient errors raise immediately.
    """
    policy = policy or RetryPolicy()
    attempt = 0
    while True:
        attempt += 1
        try:
            return fn()
        except Exception as exc:
            if not is_transient(exc) or attempt >= policy.max_attempts:
                raise
            if on_retry:
                on_retry(attempt, exc)


def classify_batch_status(*, sent: int, skipped: int, failed: int, blocked: int) -> WorkflowStatus:
    """Shared aggregator for multi-item workflows (reminders, sync, …)."""
    total = sent + skipped + failed + blocked
    if total == 0:
        return WorkflowStatus.SKIPPED
    if blocked == total:
        return WorkflowStatus.BLOCKED
    if failed + blocked == total and sent == 0 and skipped == 0:
        return WorkflowStatus.FAILED
    if failed or blocked:
        if sent or skipped:
            return WorkflowStatus.PARTIAL
        return WorkflowStatus.FAILED
    if sent == 0 and skipped == total:
        return WorkflowStatus.SKIPPED
    return WorkflowStatus.SUCCESS
