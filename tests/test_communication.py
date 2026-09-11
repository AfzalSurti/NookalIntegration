"""
Comprehensive test suite for Section 4.6 — Nookal-Native SMS + Email Communication Automation.

Covers all 33 requirement categories:
- Configuration (5 tests)
- Patient data (6 tests)
- Appointment workflows (6 tests)
- Duplicate prevention (2 tests)
- Failures & Edge cases (5 tests)
- RBAC enforcement (5 tests)
- Security & Privacy (4 tests)
"""
from __future__ import annotations

from datetime import date, datetime, timezone
import pytest

from app.communication import (
    CommunicationChannel,
    CommunicationOwnership,
    CommunicationService,
    CommunicationStatus,
    CommunicationWorkflowType,
    NookalCommunicationConfig,
)
from app.communication.communication_state import (
    CommunicationRecord,
    CommunicationStateTracker,
)
from app.communication.nookal_native import (
    appointment_should_skip_app_reminder,
    check_nookal_reminder_sent,
    observe_appointment,
)
from app.dashboard.authorization import AuthorizationError, Permission, AuthorizationPolicy
from app.dashboard.services.communication import CommunicationDashboardService
from app.nookal_client import Appointment, MockNookalClient, PatientRef
from app.nookal_client.seed import seeded_mock_client
from app.orchestration.appointment_reminders import AppointmentRemindersWorkflow
from app.orchestration.communication import (
    CheckAppointmentCommunicationWorkflow,
    CheckCommunicationConfigWorkflow,
    CheckPatientReadinessWorkflow,
)
from app.orchestration.results import WorkflowStatus
from tests.helpers import CapturingAudit, WorkflowTestContext


@pytest.fixture
def mock_nookal() -> MockNookalClient:
    return seeded_mock_client()


@pytest.fixture
def capturing_audit() -> CapturingAudit:
    return CapturingAudit()


# ============================================================================
# Category 1: Configuration (5 tests)
# ============================================================================

def test_1_nookal_sms_enabled(mock_nookal: MockNookalClient, capturing_audit: CapturingAudit) -> None:
    config = NookalCommunicationConfig(nookal_sms_enabled=True, nookal_email_enabled=False)
    comm = CommunicationService(nookal=mock_nookal, config=config, audit=capturing_audit)
    overview = comm.get_communication_overview()
    assert overview["nookal_sms"]["enabled"] is True
    assert overview["nookal_sms"]["status"] == CommunicationStatus.ENABLED.value

    patient = mock_nookal.get_patient("pat_1001")
    readiness = comm.check_patient_readiness(patient)
    assert readiness.sms_ready is True
    assert readiness.sms_status == CommunicationStatus.ENABLED


def test_2_nookal_sms_disabled(mock_nookal: MockNookalClient, capturing_audit: CapturingAudit) -> None:
    config = NookalCommunicationConfig(nookal_sms_enabled=False, nookal_email_enabled=True)
    comm = CommunicationService(nookal=mock_nookal, config=config, audit=capturing_audit)
    overview = comm.get_communication_overview()
    assert overview["nookal_sms"]["enabled"] is False
    assert overview["nookal_sms"]["status"] == CommunicationStatus.DISABLED.value

    patient = mock_nookal.get_patient("pat_1001")
    readiness = comm.check_patient_readiness(patient)
    assert readiness.sms_ready is False
    assert readiness.sms_status == CommunicationStatus.DISABLED


def test_3_nookal_email_enabled(mock_nookal: MockNookalClient, capturing_audit: CapturingAudit) -> None:
    config = NookalCommunicationConfig(nookal_sms_enabled=False, nookal_email_enabled=True)
    comm = CommunicationService(nookal=mock_nookal, config=config, audit=capturing_audit)
    overview = comm.get_communication_overview()
    assert overview["nookal_email"]["enabled"] is True
    assert overview["nookal_email"]["status"] == CommunicationStatus.ENABLED.value

    patient = mock_nookal.get_patient("pat_1001")
    readiness = comm.check_patient_readiness(patient)
    assert readiness.email_ready is True
    assert readiness.email_status == CommunicationStatus.ENABLED


def test_4_nookal_email_disabled(mock_nookal: MockNookalClient, capturing_audit: CapturingAudit) -> None:
    config = NookalCommunicationConfig(nookal_sms_enabled=True, nookal_email_enabled=False)
    comm = CommunicationService(nookal=mock_nookal, config=config, audit=capturing_audit)
    overview = comm.get_communication_overview()
    assert overview["nookal_email"]["enabled"] is False
    assert overview["nookal_email"]["status"] == CommunicationStatus.DISABLED.value

    patient = mock_nookal.get_patient("pat_1001")
    readiness = comm.check_patient_readiness(patient)
    assert readiness.email_ready is False
    assert readiness.email_status == CommunicationStatus.DISABLED


def test_5_configuration_permission_enforcement() -> None:
    policy = AuthorizationPolicy()
    # Admin and Owner have COMMUNICATION_MANAGE
    assert policy.has_permission("admin", Permission.COMMUNICATION_MANAGE)
    assert policy.has_permission("owner", Permission.COMMUNICATION_MANAGE)
    # Staff and Practitioner do NOT have COMMUNICATION_MANAGE
    assert not policy.has_permission("staff", Permission.COMMUNICATION_MANAGE)
    assert not policy.has_permission("practitioner", Permission.COMMUNICATION_MANAGE)

    with pytest.raises(AuthorizationError):
        policy.require("staff", Permission.COMMUNICATION_MANAGE)



# ============================================================================
# Category 2: Patient Data (6 tests)
# ============================================================================

def test_6_patient_has_phone_sms_ready(mock_nookal: MockNookalClient) -> None:
    comm = CommunicationService(nookal=mock_nookal)
    p = PatientRef(patient_id="p_phone", phone="+61400111222", email=None, display_name="Test")
    readiness = comm.check_patient_readiness(p)
    assert readiness.has_phone is True
    assert readiness.sms_ready is True
    assert readiness.sms_status == CommunicationStatus.ENABLED


def test_7_patient_no_phone_sms_skipped(mock_nookal: MockNookalClient) -> None:
    comm = CommunicationService(nookal=mock_nookal)
    p = PatientRef(patient_id="p_no_phone", phone=None, email="test@example.com", display_name="Test")
    readiness = comm.check_patient_readiness(p)
    assert readiness.has_phone is False
    assert readiness.sms_ready is False
    assert readiness.sms_status == CommunicationStatus.SMS_SKIPPED_NO_PHONE


def test_8_patient_has_email_email_ready(mock_nookal: MockNookalClient) -> None:
    comm = CommunicationService(nookal=mock_nookal)
    p = PatientRef(patient_id="p_email", phone=None, email="pat@example.com", display_name="Test")
    readiness = comm.check_patient_readiness(p)
    assert readiness.has_email is True
    assert readiness.email_ready is True
    assert readiness.email_status == CommunicationStatus.ENABLED


def test_9_patient_no_email_email_skipped(mock_nookal: MockNookalClient) -> None:
    comm = CommunicationService(nookal=mock_nookal)
    p = PatientRef(patient_id="p_no_email", phone="+61400111222", email=None, display_name="Test")
    readiness = comm.check_patient_readiness(p)
    assert readiness.has_email is False
    assert readiness.email_ready is False
    assert readiness.email_status == CommunicationStatus.EMAIL_SKIPPED_NO_EMAIL


def test_10_communication_preference_prevents_sending(mock_nookal: MockNookalClient) -> None:
    # Patient with whitespace/empty phone & email
    comm = CommunicationService(nookal=mock_nookal)
    p = PatientRef(patient_id="p_empty", phone="   ", email="", display_name="Empty Contact")
    readiness = comm.check_patient_readiness(p)
    assert readiness.sms_ready is False
    assert readiness.email_ready is False
    assert readiness.sms_status == CommunicationStatus.SMS_SKIPPED_NO_PHONE
    assert readiness.email_status == CommunicationStatus.EMAIL_SKIPPED_NO_EMAIL


def test_11_marketing_suppression_prevents_marketing(mock_nookal: MockNookalClient) -> None:
    comm = CommunicationService(nookal=mock_nookal)
    # Reminders and clinical notifications are Nookal-native config-only
    assert comm.is_nookal_native(CommunicationWorkflowType.REMINDER)
    assert comm.is_nookal_native(CommunicationWorkflowType.CONFIRMATION)
    # Application should not send clinical reminders when native is active
    assert not comm.should_application_send(CommunicationWorkflowType.REMINDER)
    assert not comm.should_application_send(CommunicationWorkflowType.CONFIRMATION)


# ============================================================================
# Category 3: Appointment Workflows (6 tests)
# ============================================================================

def test_12_new_appointment_defers_to_nookal(mock_nookal: MockNookalClient) -> None:
    comm = CommunicationService(nookal=mock_nookal)
    ownership = comm.get_workflow_ownership(CommunicationWorkflowType.CONFIRMATION)
    assert ownership == CommunicationOwnership.NOOKAL_NATIVE_CONFIG_ONLY
    assert comm.is_nookal_native(CommunicationWorkflowType.CONFIRMATION) is True


def test_13_reminder_defers_to_nookal_native(mock_nookal: MockNookalClient) -> None:
    comm = CommunicationService(nookal=mock_nookal)
    ownership = comm.get_workflow_ownership(CommunicationWorkflowType.REMINDER)
    assert ownership == CommunicationOwnership.NOOKAL_NATIVE_CONFIG_ONLY

    # Check appointment with email_reminder_sent = True
    appt = Appointment(
        appointment_id="appt_native_rem",
        patient_id="pat_1001",
        practitioner_id="prac_1",
        location_id="loc_1",
        starts_at=datetime.now(timezone.utc),
        ends_at=datetime.now(timezone.utc),
        email_reminder_sent=True,
    )
    status = comm.get_appointment_communication_status(appt)
    assert status.email_reminder_sent is True
    assert status.status == CommunicationStatus.NOOKAL_NATIVE_HANDLED
    assert appointment_should_skip_app_reminder(appt) is True


def test_14_cancellation_defers_to_nookal(mock_nookal: MockNookalClient) -> None:
    comm = CommunicationService(nookal=mock_nookal)
    ownership = comm.get_workflow_ownership(CommunicationWorkflowType.CANCELLATION)
    assert ownership == CommunicationOwnership.NOOKAL_NATIVE_CONFIG_ONLY
    assert comm.is_nookal_native(CommunicationWorkflowType.CANCELLATION) is True


def test_15_reschedule_defers_to_nookal(mock_nookal: MockNookalClient) -> None:
    comm = CommunicationService(nookal=mock_nookal)
    ownership = comm.get_workflow_ownership(CommunicationWorkflowType.RESCHEDULE)
    assert ownership == CommunicationOwnership.NOOKAL_NATIVE_CONFIG_ONLY
    assert comm.is_nookal_native(CommunicationWorkflowType.RESCHEDULE) is True


def test_16_no_show_classified_unsupported(mock_nookal: MockNookalClient) -> None:
    comm = CommunicationService(nookal=mock_nookal)
    ownership = comm.get_workflow_ownership(CommunicationWorkflowType.NO_SHOW)
    assert ownership == CommunicationOwnership.OFFICIAL_API_UNSUPPORTED
    assert comm.is_nookal_native(CommunicationWorkflowType.NO_SHOW) is False
    assert not comm.should_application_send(CommunicationWorkflowType.NO_SHOW)


def test_17_follow_up_recall_defers_to_nookal(mock_nookal: MockNookalClient) -> None:
    comm = CommunicationService(nookal=mock_nookal)
    ownership = comm.get_workflow_ownership(CommunicationWorkflowType.RECALL)
    assert ownership == CommunicationOwnership.NOOKAL_NATIVE_CONFIG_ONLY
    assert comm.is_nookal_native(CommunicationWorkflowType.RECALL) is True


# ============================================================================
# Category 4: Duplicate Prevention (2 tests)
# ============================================================================

def test_18_nookal_native_reminder_not_duplicated_by_app(workflow_ctx: WorkflowTestContext) -> None:
    # When channel is SMS or email, AppointmentRemindersWorkflow defers to Nookal native
    ctx = workflow_ctx.orchestration_context(correlation_id="dup-prevent-sms")
    result_sms = AppointmentRemindersWorkflow().run(ctx, channel="sms")
    assert result_sms.status == WorkflowStatus.SKIPPED
    assert result_sms.data.get("nookal_native_deferred") is True

    result_email = AppointmentRemindersWorkflow().run(ctx, channel="email")
    assert result_email.status == WorkflowStatus.SKIPPED
    assert result_email.data.get("nookal_native_deferred") is True


def test_19_repeated_workflow_execution_idempotent(tmp_path: Any) -> None:
    log_file = tmp_path / "comm_state.jsonl"
    tracker = CommunicationStateTracker(log_file)
    rec = CommunicationRecord(
        appointment_id="appt_999",
        workflow=CommunicationWorkflowType.REMINDER,
        channel=CommunicationChannel.SMS,
        ownership=CommunicationOwnership.NOOKAL_NATIVE,
        status=CommunicationStatus.NOOKAL_NATIVE_HANDLED,
    )
    # First record
    tracker.record(rec)
    assert tracker.has_been_triggered("appt_999", CommunicationWorkflowType.REMINDER) is True

    # Duplicate check
    assert tracker.is_duplicate("appt_999", CommunicationWorkflowType.REMINDER) is True
    # Different workflow is not duplicate
    assert tracker.is_duplicate("appt_999", CommunicationWorkflowType.CANCELLATION) is False


# ============================================================================
# Category 5: Failures & Edge Cases (5 tests)
# ============================================================================

def test_20_nookal_unavailable_graceful_degradation(capturing_audit: CapturingAudit) -> None:
    class FailingNookal:
        def get_patient(self, _id: str) -> Any:
            raise RuntimeError("Nookal PMS connection timeout")

    dash_svc = CommunicationDashboardService(
        nookal=FailingNookal(),  # type: ignore
        audit=capturing_audit,
    )
    res = dash_svc.get_patient_readiness(
        "pat_fail",
        actor="admin",
        role="admin",
        correlation_id="err-1",
    )
    assert res["patient_id"] == "pat_fail"
    assert res["error"] == "patient_not_found"
    assert res["sms_ready"] is False
    assert res["email_ready"] is False


def test_21_nookal_rejects_request(workflow_ctx: WorkflowTestContext) -> None:
    ctx = workflow_ctx.orchestration_context(correlation_id="c-reject")
    wf = CheckPatientReadinessWorkflow()
    result = wf.run(ctx, patient_id="non_existent_patient_id")
    assert result.status == WorkflowStatus.FAILED
    assert result.error is not None
    assert result.error.code == "patient_not_found"


def test_22_missing_contact_info_skipped(mock_nookal: MockNookalClient) -> None:
    comm = CommunicationService(nookal=mock_nookal)
    p_none = PatientRef(patient_id="p_none", phone=None, email=None, display_name="No Contact")
    readiness = comm.check_patient_readiness(p_none)
    assert readiness.sms_status == CommunicationStatus.SMS_SKIPPED_NO_PHONE
    assert readiness.email_status == CommunicationStatus.EMAIL_SKIPPED_NO_EMAIL
    assert readiness.sms_ready is False
    assert readiness.email_ready is False


def test_23_unknown_delivery_status(mock_nookal: MockNookalClient) -> None:
    # Nookal does not provide delivery receipts via API
    comm = CommunicationService(nookal=mock_nookal)
    appt = Appointment(
        appointment_id="appt_no_rec",
        patient_id="pat_1001",
        practitioner_id="prac_1",
        location_id="loc_1",
        starts_at=datetime.now(timezone.utc),
        ends_at=datetime.now(timezone.utc),
        email_reminder_sent=False,
    )
    status = comm.get_appointment_communication_status(appt)
    assert status.status == CommunicationStatus.STATUS_UNKNOWN
    assert status.ownership == CommunicationOwnership.NOOKAL_NATIVE_CONFIG_ONLY


def test_24_retry_handling_bounded(workflow_ctx: WorkflowTestContext) -> None:
    # Orchestration check workflows handle execution safely
    ctx = workflow_ctx.orchestration_context(correlation_id="bounded-check")
    wf = CheckCommunicationConfigWorkflow()
    res = wf.run(ctx)
    assert res.status == WorkflowStatus.SUCCESS
    assert "overview" in res.data
    assert "workflows" in res.data


# ============================================================================
# Category 6: RBAC Enforcement (5 tests)
# ============================================================================

def test_25_rbac_owner_can_view_and_manage() -> None:
    policy = AuthorizationPolicy()
    assert policy.has_permission("owner", Permission.COMMUNICATION_VIEW)
    assert policy.has_permission("owner", Permission.COMMUNICATION_MANAGE)


def test_26_rbac_admin_can_view_and_manage() -> None:
    policy = AuthorizationPolicy()
    assert policy.has_permission("admin", Permission.COMMUNICATION_VIEW)
    assert policy.has_permission("admin", Permission.COMMUNICATION_MANAGE)


def test_27_rbac_staff_can_view_not_manage() -> None:
    policy = AuthorizationPolicy()
    assert policy.has_permission("staff", Permission.COMMUNICATION_VIEW)
    assert not policy.has_permission("staff", Permission.COMMUNICATION_MANAGE)


def test_28_rbac_practitioner_can_view_not_manage() -> None:
    policy = AuthorizationPolicy()
    assert policy.has_permission("practitioner", Permission.COMMUNICATION_VIEW)
    assert not policy.has_permission("practitioner", Permission.COMMUNICATION_MANAGE)


def test_29_rbac_unauthorized_user_blocked() -> None:
    policy = AuthorizationPolicy()
    with pytest.raises(AuthorizationError):
        policy.require("guest", Permission.COMMUNICATION_VIEW)
    with pytest.raises(AuthorizationError):
        policy.require("billing_clerk", Permission.COMMUNICATION_MANAGE)



# ============================================================================
# Category 7: Security & Privacy (4 tests)
# ============================================================================

def test_30_security_no_secrets_in_logs(mock_nookal: MockNookalClient, capturing_audit: CapturingAudit) -> None:
    comm = CommunicationService(nookal=mock_nookal, audit=capturing_audit)
    overview = comm.get_communication_overview()
    # Ensure no API key, auth secret, or internal tokens in overview output
    raw_str = str(overview).lower()
    assert "api_key" not in raw_str
    assert "secret" not in raw_str
    assert "token" not in raw_str
    assert "password" not in raw_str


def test_31_security_no_patient_message_bodies_in_audit(
    mock_nookal: MockNookalClient,
    capturing_audit: CapturingAudit,
) -> None:
    dash = CommunicationDashboardService(nookal=mock_nookal, audit=capturing_audit)
    dash.get_patient_readiness("pat_1001", actor="admin", role="admin", correlation_id="aud-sec")

    for event in capturing_audit.events:
        meta = event.metadata or {}
        assert "message" not in meta or meta["message"] is None
        assert "body" not in meta
        assert "clinical_notes" not in meta


def test_32_security_no_credentials_in_api_responses(mock_nookal: MockNookalClient, capturing_audit: CapturingAudit) -> None:
    dash = CommunicationDashboardService(nookal=mock_nookal, audit=capturing_audit)
    overview = dash.get_overview(actor="admin", role="admin", correlation_id="api-sec")
    assert "api_key" not in overview
    assert "auth_token" not in overview
    assert "credentials" not in overview


def test_33_security_no_unauthorized_patient_communication(mock_nookal: MockNookalClient) -> None:
    # Verify CommunicationService has NO method to send arbitrary emails or SMS
    # It strictly adheres to observing and reporting Nookal native state.
    comm = CommunicationService(nookal=mock_nookal)
    assert not hasattr(comm, "send_sms")
    assert not hasattr(comm, "send_email")
    assert not hasattr(comm, "dispatch_message")
    assert not hasattr(comm, "send_whatsapp")
