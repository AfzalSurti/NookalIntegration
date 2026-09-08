from __future__ import annotations

from datetime import date

from app.nookal_client.seed import load_clinic_seed, load_seed_dict, seeded_mock_client, sync_scenarios


def test_seed_loads_expected_counts() -> None:
    client = seeded_mock_client()
    assert len(client.patients) == 4
    assert len(client.appointments) == 5
    assert len(client.referrers) == 3
    assert len(client.referrals) == 2
    assert len(client.documents) == 1
    assert client.as_of == date(2026, 9, 7)


def test_seed_has_tomorrow_appointment() -> None:
    client = seeded_mock_client()
    tomorrow = date(2026, 9, 8)
    appts = client.list_appointments(on_date=tomorrow)
    assert any(a.patient_id == "pat_1001" for a in appts)


def test_seed_patient_with_multiple_appointments() -> None:
    client = seeded_mock_client()
    appts = client.list_appointments(patient_id="pat_1002")
    assert len(appts) == 2


def test_seed_deterministic_twice() -> None:
    a = seeded_mock_client()
    b = seeded_mock_client()
    assert sorted(a.patients) == sorted(b.patients)
    assert sorted(a.appointments) == sorted(b.appointments)
    assert sorted(a.referrers) == sorted(b.referrers)
    assert sorted(a.referrals) == sorted(b.referrals)
    assert [d.document_id for d in a.documents] == [d.document_id for d in b.documents]
    assert load_seed_dict() == load_seed_dict()


def test_seed_sync_scenarios_present() -> None:
    scenarios = sync_scenarios()
    assert scenarios["exact_match_candidate"]["expected"] == "exact_match"
    assert scenarios["ambiguous_candidate"]["expected"] == "conflict_queue"
    assert scenarios["new_referrer_candidate"]["expected"] == "new_pending_confirm"


def test_load_into_existing_client() -> None:
    client = load_clinic_seed()
    assert "pat_1001" in client.patients
