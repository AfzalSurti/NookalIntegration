"""Phase E — patient, approval, document, referrer, appointment API tests."""
from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.approval import TaskStatus, TaskType
from app.shared.exceptions import KillSwitchActive
from tests.helpers.dashboard import build_dashboard_env


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    return build_dashboard_env(tmp_path, monkeypatch=monkeypatch)


@pytest.fixture()
def client(env) -> TestClient:
    return env.client()


def test_patient_search(client: TestClient, env) -> None:
    resp = env.authed(client, "GET", "/api/patients/search")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 4
    assert all("patient_id" in p for p in data["items"])


def test_suburb_filter(client: TestClient, env) -> None:
    resp = env.authed(client, "GET", "/api/patients/search", params={"suburb": "Richmond"})
    assert resp.status_code == 200
    assert resp.json()["total"] >= 1
    assert all(p["suburb"] == "Richmond" for p in resp.json()["items"])


def test_age_range_filter(client: TestClient, env) -> None:
    resp = env.authed(
        client,
        "GET",
        "/api/patients/search",
        params={"age_min": 30, "age_max": 50},
    )
    assert resp.status_code == 200
    for p in resp.json()["items"]:
        if p["age"] is not None:
            assert 30 <= p["age"] <= 50


def test_appointment_date_filter(client: TestClient, env) -> None:
    resp = env.authed(
        client,
        "GET",
        "/api/patients/search",
        params={"appointment_from": "2026-09-01", "appointment_to": "2026-09-30"},
    )
    assert resp.status_code == 200
    assert resp.json()["total"] >= 1


def test_referrer_filter(client: TestClient, env) -> None:
    resp = env.authed(
        client,
        "GET",
        "/api/patients/search",
        params={"referrer_id": "ref_john_smith"},
    )
    assert resp.status_code == 200
    assert all(p["referrer_id"] == "ref_john_smith" for p in resp.json()["items"])


def test_patient_detail_access(client: TestClient, env) -> None:
    resp = env.authed(client, "GET", "/api/patients/pat_1001")
    assert resp.status_code == 200
    body = resp.json()
    assert body["patient_id"] == "pat_1001"
    assert "appointments" in body


def test_patient_access_audited(client: TestClient, env) -> None:
    env.authed(client, "GET", "/api/patients/pat_1001")
    actions = [e.action for e in env.audit.events]
    assert "dashboard.patient_view" in actions
    view = next(e for e in env.audit.events if e.action == "dashboard.patient_view")
    assert view.target_id == "pat_1001"
    assert "name" not in (view.metadata or {})
    assert "patient_name" not in (view.metadata or {})


def test_pending_task_visible(client: TestClient, env) -> None:
    task = env.container.approval.create_task(
        task_type=TaskType.CERTIFICATE,
        patient_id="pat_1001",
        created_by="llm",
        content_draft={
            "template_id": "certificate",
            "certificate_type": "attendance",
            "status_tag": "DRAFT",
            "source_facts": {"certificate_type": "attendance", "patient_label": "patient:pat_1001"},
        },
    )
    resp = env.authed(client, "GET", "/api/approvals", role="practitioner")
    assert resp.status_code == 200
    ids = [t["id"] for t in resp.json()]
    assert task.id in ids


def test_authorized_approval(client: TestClient, env) -> None:
    task = env.container.approval.create_task(
        task_type=TaskType.CERTIFICATE,
        patient_id="pat_1001",
        created_by="llm",
        content_draft={
            "template_id": "certificate",
            "certificate_type": "attendance",
            "status_tag": "DRAFT",
            "source_facts": {"certificate_type": "attendance", "patient_label": "patient:pat_1001"},
        },
    )
    resp = env.authed(
        client,
        "POST",
        f"/api/approvals/{task.id}/approve",
        role="practitioner",
        json={"notes": "ok"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] in {"sent", "approved"}


def test_unauthorized_approval(client: TestClient, env) -> None:
    task = env.container.approval.create_task(
        task_type=TaskType.CERTIFICATE,
        patient_id="pat_1001",
        created_by="llm",
        content_draft={
            "template_id": "certificate",
            "certificate_type": "attendance",
            "status_tag": "DRAFT",
        },
    )
    resp = env.authed(
        client,
        "POST",
        f"/api/approvals/{task.id}/approve",
        role="staff",
        json={},
    )
    assert resp.status_code == 403


def test_rejection(client: TestClient, env) -> None:
    task = env.container.approval.create_task(
        task_type=TaskType.LETTER,
        patient_id="pat_1001",
        created_by="llm",
        content_draft={"template_id": "referral_thank_you", "letter_type": "referral_thank_you"},
    )
    resp = env.authed(
        client,
        "POST",
        f"/api/approvals/{task.id}/reject",
        role="practitioner",
        json={"notes": "needs rewrite"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected"


def test_duplicate_approval_prevented(client: TestClient, env) -> None:
    task = env.container.approval.create_task(
        task_type=TaskType.CERTIFICATE,
        patient_id="pat_1001",
        created_by="llm",
        content_draft={
            "template_id": "certificate",
            "certificate_type": "attendance",
            "status_tag": "DRAFT",
            "source_facts": {"certificate_type": "attendance", "patient_label": "patient:pat_1001"},
        },
    )
    first = env.authed(
        client, "POST", f"/api/approvals/{task.id}/approve", role="admin", json={}
    )
    assert first.status_code == 200
    second = env.authed(
        client, "POST", f"/api/approvals/{task.id}/approve", role="admin", json={}
    )
    assert second.status_code == 409


def test_dashboard_cannot_bypass_approval_queue(client: TestClient, env) -> None:
    """Routes must call ApprovalQueue.approve — spy on the queue method."""
    task = env.container.approval.create_task(
        task_type=TaskType.CERTIFICATE,
        patient_id="pat_1001",
        created_by="llm",
        content_draft={
            "template_id": "certificate",
            "certificate_type": "attendance",
            "status_tag": "DRAFT",
            "source_facts": {"certificate_type": "attendance", "patient_label": "patient:pat_1001"},
        },
    )
    original = env.container.approval.approve
    calls: list[str] = []

    def tracked(task_id, reviewer_id, notes=None):
        calls.append(task_id)
        return original(task_id, reviewer_id, notes=notes)

    env.container.approval.approve = tracked  # type: ignore[method-assign]
    resp = env.authed(
        client, "POST", f"/api/approvals/{task.id}/approve", role="admin", json={}
    )
    assert resp.status_code == 200
    assert calls == [task.id]


def test_referrer_exact_match_and_conflict_display(client: TestClient, env) -> None:
    # Seed a conflict into the store
    env.conflict_store.add_conflict(
        {
            "candidate": {"name": "Dr John Smith", "provider_number": "DIFF"},
            "possible_matches": ["ref_john_smith"],
            "reason": "ambiguous",
        }
    )
    resp = env.authed(client, "GET", "/api/referrers/conflicts", role="practitioner")
    assert resp.status_code == 200
    assert len(resp.json()) >= 1
    assert resp.json()[0]["possible_matches"] == ["ref_john_smith"]


def test_authorized_conflict_resolution(client: TestClient, env) -> None:
    env.conflict_store.add_new(
        {"candidate": {"name": "Dr New Referrer", "provider_number": "PN999"}, "reason": "new"}
    )
    listed = env.authed(client, "GET", "/api/referrers/conflicts", role="admin")
    conflict_id = listed.json()[0]["conflict_id"]
    resp = env.authed(
        client,
        "POST",
        f"/api/referrers/conflicts/{conflict_id}/resolve",
        role="admin",
        json={"action": "create_new"},
    )
    assert resp.status_code == 200
    assert resp.json()["action"] == "create_new"
    assert "referrer_id" in resp.json()


def test_unauthorized_conflict_resolution(client: TestClient, env) -> None:
    env.conflict_store.add_conflict(
        {
            "candidate": {"name": "Ambiguous", "provider_number": None},
            "possible_matches": ["ref_john_smith"],
            "reason": "ambiguous",
        }
    )
    listed = env.authed(client, "GET", "/api/referrers/conflicts", role="practitioner")
    conflict_id = listed.json()[0]["conflict_id"]
    resp = env.authed(
        client,
        "POST",
        f"/api/referrers/conflicts/{conflict_id}/resolve",
        role="practitioner",
        json={"action": "reject"},
    )
    assert resp.status_code == 403


def test_resolution_audited(client: TestClient, env) -> None:
    env.conflict_store.add_new(
        {"candidate": {"name": "Dr Audit Me", "provider_number": "PN111"}, "reason": "new"}
    )
    listed = env.authed(client, "GET", "/api/referrers/conflicts", role="admin")
    conflict_id = listed.json()[0]["conflict_id"]
    env.authed(
        client,
        "POST",
        f"/api/referrers/conflicts/{conflict_id}/resolve",
        role="admin",
        json={"action": "reject"},
    )
    assert any(e.action == "dashboard.referrer_resolve" for e in env.audit.events)


def test_appointment_operational_view(client: TestClient, env) -> None:
    resp = env.authed(client, "GET", "/api/appointments")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)
    assert any(a["appointment_id"] for a in resp.json())


def test_authorized_appointment_action_needs_confirmation(client: TestClient, env) -> None:
    from tests.helpers.fake_llm import FakeIntent, FakeLLM
    from app.dashboard.services.appointments import AppointmentService
    from app.dashboard.schemas import AppointmentActionRequest

    llm = FakeLLM(
        intent=FakeIntent(
            intent="cancel_appointment",
            confidence="high",
            extracted_fields={"appointment_id": "appt_2001"},
        )
    )
    svc = AppointmentService(
        nookal=env.nookal,
        messaging=env.messaging,
        approval=env.container.approval,
        audit=env.audit,
        pending_actions=env.container.pending_actions,
        clock=env.clock,
        llm=llm,
    )
    result = svc.run_action(
        actor="u_admin",
        role="admin",
        correlation_id="corr-test",
        body=AppointmentActionRequest(
            action="cancel",
            phone="+61411110001",
            message="please cancel my appointment",
        ),
    )
    assert result.status.value == "needs_confirmation"


def test_unauthorized_appointment_action(client: TestClient, env) -> None:
    resp = env.authed(
        client,
        "POST",
        "/api/appointments/actions",
        role="staff",
        json={"action": "cancel", "phone": "+61411110001", "message": "cancel"},
    )
    assert resp.status_code == 403


def test_confirmation_rules_remain_enforced(client: TestClient, env) -> None:
    from tests.helpers.fake_llm import FakeIntent, FakeLLM
    from app.dashboard.services.appointments import AppointmentService
    from app.dashboard.schemas import AppointmentActionRequest

    llm = FakeLLM(
        intent=FakeIntent(
            intent="cancel_appointment",
            confidence="high",
            extracted_fields={"appointment_id": "appt_2001"},
        )
    )
    svc = AppointmentService(
        nookal=env.nookal,
        messaging=env.messaging,
        approval=env.container.approval,
        audit=env.audit,
        pending_actions=env.container.pending_actions,
        clock=env.clock,
        llm=llm,
    )
    first = svc.run_action(
        actor="u_admin",
        role="admin",
        correlation_id="corr-1",
        body=AppointmentActionRequest(
            action="cancel",
            phone="+61411110001",
            message="cancel please",
        ),
    )
    assert first.status.value == "needs_confirmation"
    unclear = svc.run_action(
        actor="u_admin",
        role="admin",
        correlation_id="corr-2",
        body=AppointmentActionRequest(
            action="cancel",
            phone="+61411110001",
            confirmation_text="maybe",
        ),
    )
    assert unclear.status.value == "needs_human"
    assert unclear.error and unclear.error.code == "confirmation_unclear"


def test_kill_switch_blocks_mutating_appointment_action(client: TestClient, env) -> None:
    from tests.helpers.fake_llm import FakeIntent, FakeLLM
    from app.dashboard.services.appointments import AppointmentService
    from app.dashboard.schemas import AppointmentActionRequest

    llm = FakeLLM(
        intent=FakeIntent(
            intent="cancel_appointment",
            confidence="high",
            extracted_fields={"appointment_id": "appt_2001"},
        )
    )
    svc = AppointmentService(
        nookal=env.nookal,
        messaging=env.messaging,
        approval=env.container.approval,
        audit=env.audit,
        pending_actions=env.container.pending_actions,
        clock=env.clock,
        llm=llm,
    )
    pending = svc.run_action(
        actor="u_admin",
        role="admin",
        correlation_id="corr-ks-1",
        body=AppointmentActionRequest(
            action="cancel",
            phone="+61411110001",
            message="cancel please",
        ),
    )
    assert pending.status.value == "needs_confirmation"
    env.container.kill_switch_path.write_text("on", encoding="utf-8")
    confirmed = svc.run_action(
        actor="u_admin",
        role="admin",
        correlation_id="corr-ks-2",
        body=AppointmentActionRequest(
            action="cancel",
            phone="+61411110001",
            confirmation_text="YES",
            pending_action_id=(pending.data or {}).get("pending_action_id"),
        ),
    )
    assert confirmed.status.value == "blocked"
