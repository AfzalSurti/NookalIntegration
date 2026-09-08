from __future__ import annotations

from pathlib import Path


class AutomationError(Exception):
    """Base for all app-level failures."""


class ConfigError(AutomationError):
    pass


class KillSwitchActive(AutomationError):
    """Outbound write/send blocked by the kill switch."""

    def __init__(self, operation: str) -> None:
        self.operation = operation
        super().__init__(f"kill switch active — blocked: {operation}")


class AuditRejected(AutomationError):
    """Caller tried to put sensitive content into the audit log."""


class StateTransitionError(AutomationError):
    """Invalid Task status change (e.g. draft → sent)."""


class ApprovalError(AutomationError):
    pass


class MessagingError(AutomationError):
    pass


class IdempotencyConflict(MessagingError):
    """Message with this idempotency key was already sent."""


class PermissionDenied(MessagingError):
    pass


class LLMError(AutomationError):
    pass


class NookalError(AutomationError):
    """Base for Nookal client failures."""


class NookalAuthError(NookalError):
    pass


class NookalNotFound(NookalError):
    pass


class NookalValidationError(NookalError):
    pass


class NookalRateLimit(NookalError):
    pass


class NookalServerError(NookalError):
    pass


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]
