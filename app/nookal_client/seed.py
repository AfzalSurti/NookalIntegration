"""
Load deterministic synthetic clinic data into MockNookalClient.

Data lives under data/testdata/ — synthetic only, never real patients.
This module does not call the live Nookal API.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

from app.nookal_client.client import (
    Appointment,
    DocumentMeta,
    MockNookalClient,
    PatientRef,
    Referral,
    Referrer,
)
from app.shared.exceptions import repo_root

AuditFn = Callable[..., Any]

SEED_FILENAME = "clinic_seed.json"
DEFAULT_AS_OF = date(2026, 9, 7)


def seed_path() -> Path:
    return repo_root() / "data" / "testdata" / SEED_FILENAME


def load_seed_dict(path: Path | None = None) -> dict[str, Any]:
    target = path or seed_path()
    with target.open(encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("clinic seed must be a JSON object")
    return data


def _parse_date(value: str | None) -> date | None:
    if value is None:
        return None
    return date.fromisoformat(value)


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def load_clinic_seed(
    client: MockNookalClient | None = None,
    *,
    path: Path | None = None,
    audit: AuditFn | None = None,
) -> MockNookalClient:
    """
    Populate a MockNookalClient from the synthetic seed file.

    Returns the same client instance for chaining. Always uses fixed dates
    from the seed — never wall-clock "tomorrow".
    """
    data = load_seed_dict(path)
    as_of = _parse_date(data.get("as_of")) or DEFAULT_AS_OF

    if client is None:
        client = MockNookalClient(audit=audit or (lambda *a, **kw: None), as_of=as_of)
    else:
        client.as_of = as_of
        if audit is not None:
            client._audit = audit

    for row in data.get("patients") or []:
        client.seed_patient(
            PatientRef(
                patient_id=str(row["patient_id"]),
                phone=row.get("phone"),
                email=row.get("email"),
                display_name=row.get("display_name"),
                date_of_birth=_parse_date(row.get("date_of_birth")),
                suburb=row.get("suburb"),
                referrer_id=row.get("referrer_id"),
                last_appointment_date=_parse_date(row.get("last_appointment_date")),
            )
        )

    for row in data.get("referrers") or []:
        client.seed_referrer(
            Referrer(
                referrer_id=str(row["referrer_id"]),
                name=str(row["name"]),
                provider_number=row.get("provider_number"),
                raw=dict(row),
            )
        )

    for row in data.get("appointments") or []:
        client.seed_appointment(
            Appointment(
                appointment_id=str(row["appointment_id"]),
                patient_id=str(row["patient_id"]),
                starts_at=_parse_datetime(row["starts_at"]),
                status=row.get("status"),
                practitioner_id=(
                    str(row["practitioner_id"]) if row.get("practitioner_id") else None
                ),
                raw=dict(row),
            )
        )

    for row in data.get("referrals") or []:
        client.seed_referral(
            Referral(
                referral_id=str(row["referral_id"]),
                patient_id=str(row["patient_id"]),
                referrer_id=str(row["referrer_id"]),
                recorded_on=_parse_date(row["recorded_on"]) or DEFAULT_AS_OF,
                notes_ref=row.get("notes_ref"),
            )
        )

    for row in data.get("documents") or []:
        client.seed_document(
            DocumentMeta(
                document_id=str(row["document_id"]),
                patient_id=str(row["patient_id"]),
                title=row.get("title"),
            )
        )

    return client


def seeded_mock_client(
    *,
    path: Path | None = None,
    audit: AuditFn | None = None,
) -> MockNookalClient:
    return load_clinic_seed(path=path, audit=audit)


def sync_scenarios(path: Path | None = None) -> dict[str, Any]:
    """Referrer-sync fixtures for Phase F — not auto-loaded into the mock store."""
    return dict(load_seed_dict(path).get("sync_scenarios") or {})
