from __future__ import annotations

from datetime import date, datetime, timezone

from app.nookal_client import (
    Appointment,
    MockNookalClient,
    PatientRef,
    Referral,
    Referrer,
)


def _client() -> MockNookalClient:
    return MockNookalClient(audit=lambda *a, **kw: None, as_of=date(2026, 9, 7))


def test_seed_and_get_referral() -> None:
    client = _client()
    client.seed_patient(PatientRef(patient_id="p1"))
    client.seed_referrer(Referrer(referrer_id="r1", name="Dr Exact"))
    ref = Referral(
        referral_id="ref_1",
        patient_id="p1",
        referrer_id="r1",
        recorded_on=date(2026, 8, 1),
    )
    client.seed_referral(ref)
    assert client.get_referral("ref_1") == ref


def test_list_referrals_filters() -> None:
    client = _client()
    client.seed_referral(
        Referral("ref_a", "p1", "r1", date(2026, 8, 1))
    )
    client.seed_referral(
        Referral("ref_b", "p2", "r1", date(2026, 8, 2))
    )
    client.seed_referral(
        Referral("ref_c", "p1", "r2", date(2026, 8, 3))
    )
    assert [r.referral_id for r in client.list_referrals(patient_id="p1")] == [
        "ref_a",
        "ref_c",
    ]
    assert [r.referral_id for r in client.list_referrals(referrer_id="r1")] == [
        "ref_a",
        "ref_b",
    ]


def test_search_patients_by_suburb() -> None:
    client = _client()
    client.seed_patient(PatientRef("p1", suburb="Richmond"))
    client.seed_patient(PatientRef("p2", suburb="Carlton"))
    hits = client.search_patients(suburb="richmond")
    assert [p.patient_id for p in hits] == ["p1"]


def test_search_patients_by_age_range() -> None:
    client = _client()
    # as_of = 2026-09-07 → ages 30 and 45
    client.seed_patient(
        PatientRef("p_young", date_of_birth=date(1996, 1, 1), suburb="Richmond")
    )
    client.seed_patient(
        PatientRef("p_mid", date_of_birth=date(1981, 1, 1), suburb="Richmond")
    )
    hits = client.search_patients(age_min=40, age_max=50)
    assert [p.patient_id for p in hits] == ["p_mid"]


def test_search_patients_by_appointment_date_range() -> None:
    client = _client()
    client.seed_patient(PatientRef("p1"))
    client.seed_patient(PatientRef("p2"))
    client.seed_appointment(
        Appointment(
            "a1",
            "p1",
            datetime(2026, 9, 10, 10, 0, tzinfo=timezone.utc),
        )
    )
    client.seed_appointment(
        Appointment(
            "a2",
            "p2",
            datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc),
        )
    )
    hits = client.search_patients(
        appointment_from=date(2026, 9, 1),
        appointment_to=date(2026, 9, 30),
    )
    assert [p.patient_id for p in hits] == ["p1"]


def test_search_patients_by_referrer() -> None:
    client = _client()
    client.seed_patient(PatientRef("p1", referrer_id="r1"))
    client.seed_patient(PatientRef("p2", referrer_id="r2"))
    hits = client.search_patients(referrer_id="r1")
    assert [p.patient_id for p in hits] == ["p1"]


def test_seed_appointment_updates_last_appointment_date() -> None:
    client = _client()
    client.seed_patient(PatientRef("p1"))
    client.seed_appointment(
        Appointment(
            "a1",
            "p1",
            datetime(2026, 9, 10, 10, 0, tzinfo=timezone.utc),
        )
    )
    assert client.get_patient("p1").last_appointment_date == date(2026, 9, 10)
