"""Deterministic patient identity helpers for messaging workflows."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.nookal_client import NookalClient, PatientRef

IdentityStatus = Literal["matched", "not_found", "ambiguous"]


@dataclass(frozen=True)
class IdentityResolution:
    status: IdentityStatus
    patient: PatientRef | None = None
    match_count: int = 0


def resolve_patient_by_phone(nookal: NookalClient, phone: str) -> IdentityResolution:
    hits = nookal.find_patient_by_phone(phone)
    if len(hits) == 0:
        return IdentityResolution(status="not_found", match_count=0)
    if len(hits) > 1:
        return IdentityResolution(status="ambiguous", match_count=len(hits))
    return IdentityResolution(status="matched", patient=hits[0], match_count=1)


def is_affirmative(text: str) -> bool:
    normalized = text.strip().upper()
    return normalized in {"YES", "Y", "CONFIRM", "CONFIRMED"}


def is_negative(text: str) -> bool:
    normalized = text.strip().upper()
    return normalized in {"NO", "N", "CANCEL", "STOP"}
