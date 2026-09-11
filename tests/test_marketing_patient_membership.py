from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.marketing import (
    AudienceBuilder,
    ConsentStore,
    MarketingFilter,
    MarketingFilterType,
    MarketingList,
    SuppressionStore,
)
from app.nookal_client import MockNookalClient, PatientRef
from tests.helpers.dashboard import TEST_USERS, build_dashboard_env


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    return build_dashboard_env(tmp_path, monkeypatch=monkeypatch)


@pytest.fixture()
def client(env) -> TestClient:
    return env.client()


def _login(client: TestClient, role: str = "admin") -> dict[str, str]:
    resp = client.post(
        "/api/auth/login",
        json={"username": TEST_USERS[role][0].username, "password": TEST_USERS[role][1]},
    )
    assert resp.status_code == 200, resp.text
    return {"X-CSRF-Token": resp.json()["csrf_token"]}


def test_audience_with_explicit_patient_ids_and_exclusions(tmp_path: Path) -> None:
    nookal = MockNookalClient()
    nookal.seed_patient(
        PatientRef(
            patient_id="p1",
            first_name="Alice",
            email="alice@example.com",
            suburb="Richmond",
        )
    )
    nookal.seed_patient(
        PatientRef(
            patient_id="p2",
            first_name="Bob",
            email="bob@example.com",
            suburb="Richmond",
        )
    )
    nookal.seed_patient(
        PatientRef(
            patient_id="p3",
            first_name="Charlie",
            email="charlie@example.com",
            suburb="Fitzroy",
        )
    )

    consent_store = ConsentStore(tmp_path / "consent.jsonl")
    suppression_store = SuppressionStore(tmp_path / "suppression.jsonl")
    for pid in ["p1", "p2", "p3"]:
        consent_store.grant(pid)

    builder = AudienceBuilder(nookal, consent_store, suppression_store)

    # List with Suburb=Richmond BUT excluding p2
    suburb_filter = MarketingFilter(MarketingFilterType.SUBURB, "Richmond")
    exclude_filter = MarketingFilter(MarketingFilterType.EXCLUDED_PATIENT_IDS, ["p2"])

    mlist = MarketingList(
        id="l1",
        name="Richmond without Bob",
        description="",
        filter_definition=(suburb_filter, exclude_filter),
    )

    result = builder.build_audience(mlist)
    assert result.candidate_count == 1  # p1 only (p2 was removed)
    assert result.eligible_count == 1

    recipients = builder.get_eligible_recipients(mlist)
    assert len(recipients) == 1
    assert recipients[0].patient_id == "p1"


def test_audience_with_explicitly_included_patients_only(tmp_path: Path) -> None:
    nookal = MockNookalClient()
    nookal.seed_patient(
        PatientRef(
            patient_id="p10",
            first_name="David",
            email="david@example.com",
            suburb="Collingwood",
        )
    )
    nookal.seed_patient(
        PatientRef(
            patient_id="p20",
            first_name="Eve",
            email="eve@example.com",
            suburb="South Yarra",
        )
    )

    consent_store = ConsentStore(tmp_path / "consent.jsonl")
    suppression_store = SuppressionStore(tmp_path / "suppression.jsonl")
    consent_store.grant("p10")
    consent_store.grant("p20")

    builder = AudienceBuilder(nookal, consent_store, suppression_store)

    # Explicit list of patient IDs
    include_filter = MarketingFilter(MarketingFilterType.PATIENT_IDS, ["p10", "p20"])
    mlist = MarketingList(
        id="l2",
        name="Hand-picked VIPs",
        description="",
        filter_definition=(include_filter,),
    )

    result = builder.build_audience(mlist)
    assert result.candidate_count == 2
    assert result.eligible_count == 2


def test_api_create_list_with_add_and_remove_patients(client: TestClient) -> None:
    headers = _login(client, role="admin")

    # Create list with explicitly included patient pat_1001 and excluded patient pat_1002
    resp = client.post(
        "/api/marketing/lists",
        headers=headers,
        json={
            "name": "Customized Patient List",
            "description": "Selected patients",
            "patient_ids": ["pat_1001"],
            "excluded_patient_ids": ["pat_1002"],
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["name"] == "Customized Patient List"
    filters = data["filter_definition"]
    filter_types = [f["filter_type"] for f in filters]
    assert "patient_ids" in filter_types
    assert "excluded_patient_ids" in filter_types


def test_api_preview_marketing_list(client: TestClient) -> None:
    headers = _login(client, role="admin")

    resp = client.post(
        "/api/marketing/lists/preview",
        headers=headers,
        json={
            "patient_ids": ["pat_1001"],
            "excluded_patient_ids": [],
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "candidate_count" in data
    assert "eligible_count" in data
    assert "patients" in data
    assert len(data["patients"]) >= 1
    assert data["patients"][0]["patient_id"] == "pat_1001"
