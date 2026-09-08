"""Central permission identifiers — do not scatter string literals in routes."""
from __future__ import annotations

from enum import StrEnum


class Permission(StrEnum):
    PATIENT_VIEW = "patient:view"
    PATIENT_SEARCH = "patient:search"
    APPOINTMENT_VIEW = "appointment:view"
    APPOINTMENT_CHANGE = "appointment:change"
    APPROVAL_VIEW = "approval:view"
    APPROVAL_APPROVE = "approval:approve"
    APPROVAL_REJECT = "approval:reject"
    REFERRER_VIEW = "referrer:view"
    REFERRER_RESOLVE = "referrer:resolve"
    AUDIT_VIEW = "audit:view"
    MARKETING_VIEW = "marketing:view"
    MARKETING_MANAGE = "marketing:manage"
    SYSTEM_VIEW = "system:view"
    SYSTEM_KILL_SWITCH = "system:kill_switch"
