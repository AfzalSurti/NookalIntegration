"""
F3 — Suppression model.

Suppression as a first-class concept.

Suppression OVERRIDES old audience snapshots.
Example: campaign created yesterday cannot send to patient who unsubscribed today.

Support at minimum:
- unsubscribe
- consent revoked
- administrative suppression
- missing/invalid address

Do not allow staff to silently override explicit opt-outs.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any


class SuppressionReason(str, Enum):
    """Reason for suppression."""

    UNSUBSCRIBE = "unsubscribe"  # explicit patient opt-out
    CONSENT_REVOKED = "consent_revoked"  # formal consent withdrawal
    ADMINISTRATIVE = "administrative"  # staff or system override
    INVALID_EMAIL = "invalid_email"  # missing or malformed
    BOUNCED = "bounced"  # provider permanent failure


@dataclass(frozen=True)
class SuppressionRecord:
    """
    Immutable suppression state for a patient.

    Once suppressed, patient will not receive marketing until explicitly unsuppressed.
    Thread-safe: create new records rather than mutate existing ones.
    """

    patient_id: str
    reason: SuppressionReason
    suppressed_at: str  # ISO8601
    suppressed_by: str = "system"
    notes: str = ""  # optional reason/metadata
    expires_at: str | None = None  # optional expiry (e.g., administrative suppression)

    def is_active(self, now: str | None = None) -> bool:
        """
        Is this suppression currently active?

        If expires_at is set and now >= expires_at, suppression has expired.
        For Phase F, expiry is optional and rarely used.
        """
        if self.expires_at is None:
            return True
        if now is None:
            return True  # conservative: assume active if no time provided
        return now < self.expires_at

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["reason"] = self.reason.value
        return data


class SuppressionPolicy:
    """
    Policy for determining suppression eligibility.

    Can be subclassed if suppression rules change.
    """

    def is_suppressed(self, suppression: SuppressionRecord | None, now: str | None = None) -> bool:
        """
        Is the patient suppressed?

        If suppression record exists and is active, patient is suppressed.
        Missing suppression record → not suppressed.
        """
        if not suppression:
            return False
        return suppression.is_active(now)
