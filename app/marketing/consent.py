"""
F2 — Consent model.

Explicit deterministic consent state.

DO NOT infer consent from:
- existing patient status
- having an email address
- previous transactional communication
- attending appointments
- being a current patient

Use explicit states: GRANTED, NOT_GRANTED, REVOKED.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class ConsentState(str, Enum):
    """Explicit marketing consent state."""

    GRANTED = "granted"
    NOT_GRANTED = "not_granted"
    REVOKED = "revoked"


@dataclass(frozen=True)
class ConsentRecord:
    """
    Immutable consent state for a patient.

    Thread-safe: create new records rather than mutate existing ones.
    """

    patient_id: str
    state: ConsentState
    changed_at: str  # ISO8601
    changed_by: str = "system"
    notes: str = ""  # optional reason/audit trail

    def is_granted(self) -> bool:
        """Patient has explicitly granted consent for marketing."""
        return self.state == ConsentState.GRANTED

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["state"] = self.state.value
        return data


class ConsentPolicy:
    """
    Policy for determining marketing eligibility based on consent.

    Can be subclassed or configured if policy changes later.
    """

    def is_eligible(self, consent: ConsentRecord | None) -> bool:
        """
        Is the patient eligible for marketing based on consent?

        Default: requires explicit GRANTED state.
        Missing consent record → NOT_GRANTED → ineligible.
        """
        if not consent:
            return False
        return consent.is_granted()
