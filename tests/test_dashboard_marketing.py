from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.helpers.dashboard import TEST_USERS, build_dashboard_env


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    return build_dashboard_env(tmp_path, monkeypatch=monkeypatch)


@pytest.fixture()
def client(env) -> TestClient:
    return env.client()


def test_marketing_list_api_available(client: TestClient) -> None:
    csrf = client.post(
        "/api/auth/login",
        json={"username": TEST_USERS["admin"][0].username, "password": TEST_USERS["admin"][1]},
    ).json()["csrf_token"]
    resp = client.get("/api/marketing/lists", headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_marketing_create_list_and_campaign(client: TestClient) -> None:
    resp = client.post(
        "/api/auth/login",
        json={"username": TEST_USERS["admin"][0].username, "password": TEST_USERS["admin"][1]},
    )
    csrf = resp.json()["csrf_token"]
    headers = {"X-CSRF-Token": csrf}

    list_resp = client.post(
        "/api/marketing/lists",
        headers=headers,
        json={"name": "Redfern patients", "description": "Test list", "filter_definition": []},
    )
    assert list_resp.status_code == 200, list_resp.text
    list_id = list_resp.json()["id"]

    campaign_resp = client.post(
        "/api/marketing/campaigns",
        headers=headers,
        json={
            "name": "Welcome campaign",
            "subject": "Hello there",
            "template_id": "tpl-welcome",
            "marketing_list_id": list_id,
        },
    )
    assert campaign_resp.status_code == 200, campaign_resp.text
    assert campaign_resp.json()["status"] == "draft"


def test_marketing_page_requires_permission(client: TestClient, env) -> None:
    staff_csrf = env.login(client, "staff")
    resp = client.get("/marketing", headers={"X-CSRF-Token": staff_csrf})
    assert resp.status_code == 200
