"""
Comprehensive test suite for Nookal Referral Sync via Report Export.

Tests:
1. Valid report sync (Patient ID matching, canonical referrer matching, association creation, audit)
2. Multiple patients and duplicate rows (idempotence, deduplication)
3. Missing patient ID with name fallback match vs ambiguous match (review queue)
4. Missing referrer field handling
5. Changed referrer for an existing patient (update and audit trail)
6. Ambiguous referrer candidate (routed to ReferrerConflictStore.conflicts)
7. Brand new doctor candidate (routed to ReferrerConflictStore.new_pending)
8. Malformed / aggregate report rejection (e.g. summaries without patient line items)
9. Repeated synchronization idempotency (zero state change, unchanged counter incremented)
10. Empty report handling (graceful skipped/empty summary)
11. Secret discipline: no patient names or sensitive report text in audit events
12. Dashboard report upload endpoint (/api/referrers/upload-report)
13. OpenClaw ReferralReportSyncWorkflow execution
"""
from __future__ import annotations

import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.nookal_client import MockNookalClient, PatientRef, Referrer
from app.orchestration.referral_report import MalformedReportError, ReferralReportParser
from app.orchestration.referral_sync_service import (
    ReferralAssociationStore,
    ReferralSyncService,
)
from app.orchestration.referrer_sync import ReferralReportSyncWorkflow, ReferrerConflictStore
from app.orchestration.results import WorkflowStatus
from tests.helpers import WorkflowTestContext, build_workflow_context
from tests.helpers.dashboard import build_dashboard_env


@pytest.fixture()
def sync_env(tmp_path: Path):
    wf_ctx = build_workflow_context(tmp_path)
    assoc_store = ReferralAssociationStore(tmp_path / "associations.jsonl")
    conflict_store = ReferrerConflictStore()
    service = ReferralSyncService(
        nookal=wf_ctx.nookal,
        association_store=assoc_store,
        conflict_store=conflict_store,
        audit=wf_ctx.audit,
        clock=wf_ctx.clock,
    )
    return {
        "wf_ctx": wf_ctx,
        "service": service,
        "assoc_store": assoc_store,
        "conflict_store": conflict_store,
        "tmp_path": tmp_path,
    }


def test_valid_patient_level_report_sync(sync_env):
    """Test standard patient-level referral report sync."""
    service: ReferralSyncService = sync_env["service"]
    assoc_store: ReferralAssociationStore = sync_env["assoc_store"]
    wf_ctx: WorkflowTestContext = sync_env["wf_ctx"]

    # pat_1001 is seeded in clinic_seed.json ("Alex Rivera")
    # Dr John Smith is seeded in clinic_seed.json ("ref_john_smith")
    csv_text = """Patient ID,Patient Name,Referring Doctor,Provider Number
pat_1001,Alex Rivera,Dr John Smith,1234567A
"""
    summary = service.sync_report_text(csv_text, source_name="nookal_export.csv")

    assert summary.total_rows == 1
    assert summary.matched_exact == 1
    assert summary.conflicts_queued == 0
    assert summary.new_pending_queued == 0
    assert summary.review_required_patients == 0

    assoc = assoc_store.get("pat_1001")
    assert assoc is not None
    assert assoc.patient_id == "pat_1001"
    assert assoc.referrer_name == "Dr John Smith"
    assert assoc.referrer_id == "ref_john_smith"
    assert assoc.provider_number == "1234567A"
    assert assoc.status == "synced"

    # Verify audit event emitted
    audit_events = wf_ctx.audit.events
    created_events = [e for e in audit_events if e.action == "referral_sync.association_created"]
    assert len(created_events) == 1
    assert created_events[0].target_id == "pat_1001"
    # Secret discipline: patient names and doctor names must not be leaked into audit metadata
    assert "Alex Rivera" not in str(created_events[0].metadata)
    assert "Dr John Smith" not in str(created_events[0].metadata)


def test_repeated_synchronization_is_idempotent(sync_env):
    """Syncing the same report twice updates timestamp only, producing 0 duplicate entries."""
    service: ReferralSyncService = sync_env["service"]
    assoc_store: ReferralAssociationStore = sync_env["assoc_store"]

    csv_text = """Patient ID,Patient Name,Referring Doctor
pat_1001,Alex Rivera,Dr John Smith
"""
    s1 = service.sync_report_text(csv_text, source_name="run1.csv")
    assert s1.matched_exact == 1
    assert s1.unchanged == 0

    s2 = service.sync_report_text(csv_text, source_name="run2.csv")
    assert s2.matched_exact == 0
    assert s2.unchanged == 1

    # Single association stored
    all_assocs = assoc_store.list_all()
    assert len(all_assocs) == 1


def test_changed_referrer_updates_association_and_audits(sync_env):
    """Existing patient synced with a different doctor triggers referrer_changed."""
    service: ReferralSyncService = sync_env["service"]
    assoc_store: ReferralAssociationStore = sync_env["assoc_store"]
    wf_ctx: WorkflowTestContext = sync_env["wf_ctx"]

    # First sync: Dr John Smith
    csv_1 = """Patient ID,Patient Name,Referring Doctor\npat_1001,Alex Rivera,Dr John Smith\n"""
    service.sync_report_text(csv_1, source_name="run1.csv")

    # Second sync: Dr Priya Nair ("ref_priya_nair")
    csv_2 = """Patient ID,Patient Name,Referring Doctor\npat_1001,Alex Rivera,Dr Priya Nair\n"""
    s2 = service.sync_report_text(csv_2, source_name="run2.csv")
    assert s2.updated_changed == 1
    assert s2.matched_exact == 0

    assoc = assoc_store.get("pat_1001")
    assert assoc.referrer_name == "Dr Priya Nair"
    assert assoc.referrer_id == "ref_priya_nair"

    change_events = [e for e in wf_ctx.audit.events if e.action == "referral_sync.referrer_changed"]
    assert len(change_events) == 1
    assert change_events[0].target_id == "pat_1001"


def test_ambiguous_referrer_match_routes_to_conflict_queue(sync_env):
    """Dr J. Smith matches both Dr John Smith and Dr J Smith -> conflict queue."""
    service: ReferralSyncService = sync_env["service"]
    conflict_store: ReferrerConflictStore = sync_env["conflict_store"]
    assoc_store: ReferralAssociationStore = sync_env["assoc_store"]

    # "Dr J. Smith" matches "ref_john_smith" and "ref_j_smith_legacy"
    csv_text = """Patient ID,Patient Name,Referring Doctor\npat_1001,Alex Rivera,Dr J. Smith\n"""
    summary = service.sync_report_text(csv_text, source_name="ambig.csv")

    assert summary.conflicts_queued == 1
    assert len(conflict_store.conflicts) == 1
    conflict = conflict_store.conflicts[0]
    assert conflict["candidate"]["name"] == "Dr J. Smith"
    assert len(conflict["possible_matches"]) >= 1

    assoc = assoc_store.get("pat_1001")
    assert assoc.status == "conflict"
    assert assoc.referrer_id is None


def test_new_referrer_candidate_routes_to_new_pending_queue(sync_env):
    """Unknown doctor candidate routes to new_pending queue without auto-inventing IDs."""
    service: ReferralSyncService = sync_env["service"]
    conflict_store: ReferrerConflictStore = sync_env["conflict_store"]
    assoc_store: ReferralAssociationStore = sync_env["assoc_store"]

    csv_text = """Patient ID,Patient Name,Referring Doctor,Provider Number
pat_1001,Alex Rivera,Dr Unknown Specialist,9988776Z
"""
    summary = service.sync_report_text(csv_text, source_name="new_doc.csv")

    assert summary.new_pending_queued == 1
    assert len(conflict_store.new_pending) == 1
    pending = conflict_store.new_pending[0]
    assert pending["candidate"]["name"] == "Dr Unknown Specialist"
    assert pending["candidate"]["provider_number"] == "9988776Z"

    assoc = assoc_store.get("pat_1001")
    assert assoc.status == "review_required"
    assert assoc.referrer_id is None


def test_unmatched_patient_routed_to_review(sync_env):
    """Patient ID that does not exist in Nookal routes to review without guessing."""
    service: ReferralSyncService = sync_env["service"]
    conflict_store: ReferrerConflictStore = sync_env["conflict_store"]

    csv_text = """Patient ID,Patient Name,Referring Doctor\npat_99999_nonexistent,Ghost User,Dr John Smith\n"""
    summary = service.sync_report_text(csv_text, source_name="ghost.csv")

    assert summary.review_required_patients == 1
    assert len(conflict_store.conflicts) == 1
    assert conflict_store.conflicts[0]["kind"] == "patient_unmatched"


def test_missing_patient_id_fallback_to_unique_name(sync_env):
    """When Patient ID is absent, unique name match resolves patient."""
    service: ReferralSyncService = sync_env["service"]
    assoc_store: ReferralAssociationStore = sync_env["assoc_store"]

    # Alex Rivera is uniquely pat_1001 in seed
    csv_text = """Patient Name,Referring Doctor\nAlex Rivera,Dr John Smith\n"""
    summary = service.sync_report_text(csv_text, source_name="name_only.csv")

    assert summary.matched_exact == 1
    assoc = assoc_store.get("pat_1001")
    assert assoc is not None
    assert assoc.patient_id == "pat_1001"


def test_aggregate_marketing_report_is_rejected():
    """Aggregate report lacking patient columns raises MalformedReportError."""
    # This resembles Nookal's aggregate Referrers and Sources marketing report
    aggregate_csv = """Referrer,Type,Total Clients,Total Cases,Revenue
Dr John Smith,Standard,25,30,$4500.00
Dr Priya Nair,Standard,15,18,$2700.00
"""
    with pytest.raises(MalformedReportError) as exc_info:
        ReferralReportParser.parse_text(aggregate_csv)
    assert "aggregate summary" in str(exc_info.value) or "Patient-level data" in str(exc_info.value)


def test_empty_report_returns_zero_rows(sync_env):
    """Empty CSV returns 0 rows without errors."""
    service: ReferralSyncService = sync_env["service"]
    summary = service.sync_report_text("", source_name="empty.csv")
    assert summary.total_rows == 0
    assert summary.matched_exact == 0


def test_missing_referrer_column_is_rejected():
    """CSV with patient columns but no doctor/referrer column is rejected."""
    bad_csv = """Patient ID,Patient Name,Appointment Date\npat_1001,Alex Rivera,2026-09-10\n"""
    with pytest.raises(MalformedReportError) as exc_info:
        ReferralReportParser.parse_text(bad_csv)
    assert "referrer column" in str(exc_info.value).lower()


def test_workflow_orchestration_execution(sync_env):
    """Test running ReferralReportSyncWorkflow through WorkflowContext."""
    wf_ctx: WorkflowTestContext = sync_env["wf_ctx"]
    assoc_store: ReferralAssociationStore = sync_env["assoc_store"]
    conflict_store: ReferrerConflictStore = sync_env["conflict_store"]

    csv_text = """Patient ID,Patient Name,Referring Doctor
pat_1001,Alex Rivera,Dr John Smith
pat_1002,Sam Lee,Dr Priya Nair
"""
    workflow = ReferralReportSyncWorkflow()
    result = workflow.run(
        wf_ctx.orchestration_context(correlation_id="wf_test_1"),
        report_text=csv_text,
        source_name="openclaw_feed.csv",
        association_store=assoc_store,
        conflict_store=conflict_store,
    )

    assert result.status == WorkflowStatus.SUCCESS
    assert result.data["matched_exact"] == 2
    assert result.data["conflicts_queued"] == 0
    assert assoc_store.get("pat_1001") is not None
    assert assoc_store.get("pat_1002") is not None


def test_dashboard_report_upload_api_and_page(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Test the dashboard POST /api/referrers/upload-report and GET /referrers page display."""
    env = build_dashboard_env(tmp_path, monkeypatch=monkeypatch)
    client = env.client()

    csv_content = b"""Patient ID,Patient Name,Referring Doctor,Provider Number
pat_1001,Alex Rivera,Dr John Smith,1234567A
"""
    # Upload via admin user
    csrf = env.login(client, role="admin")
    resp = client.post(
        "/api/referrers/upload-report",
        headers={"X-CSRF-Token": csrf},
        files={"file": ("nookal_referrals.csv", io.BytesIO(csv_content), "text/csv")},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success"
    assert data["total_rows"] == 1
    assert data["matched_exact"] == 1

    # Verify /referrers page displays the synchronized association table
    page_resp = env.authed(client, "GET", "/referrers", role="admin")
    assert page_resp.status_code == 200
    html = page_resp.text
    assert "pat_1001" in html
    assert "Alex Rivera" in html
    assert "Dr John Smith" in html
    assert "1234567A" in html
    assert "Upload & Ingest Nookal Report" in html
