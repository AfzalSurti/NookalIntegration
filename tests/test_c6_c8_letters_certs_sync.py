from __future__ import annotations

from app.approval import TaskStatus, TaskType
from app.orchestration.certificates import CertificateRequestWorkflow
from app.orchestration.referral_letters import ReferralThankYouWorkflow
from app.orchestration.referrer_sync import ReferrerConflictStore, ReferrerSyncWorkflow
from app.orchestration.results import WorkflowStatus
from app.nookal_client.seed import sync_scenarios
from tests.helpers import WorkflowTestContext
from tests.helpers.fake_llm import FakeDraft, FakeIntent, FakeLLM


def test_c6_certificate_creates_pending_task(workflow_ctx: WorkflowTestContext) -> None:
    llm = FakeLLM(
        intent=FakeIntent(
            intent="request_certificate",
            confidence="high",
            extracted_fields={"certificate_type": "attendance"},
        )
    )
    result = CertificateRequestWorkflow().run(
        workflow_ctx.orchestration_context(correlation_id="c6-ok", llm=llm),
        phone="+61411110001",
        message="I need a certificate",
    )
    assert result.status == WorkflowStatus.SUCCESS
    task = workflow_ctx.approval.get(result.data["task_id"])
    assert task.type == TaskType.CERTIFICATE
    assert task.status == TaskStatus.PENDING_REVIEW
    # No document save / message side effects before approval
    assert workflow_ctx.nookal.documents == [
        d for d in workflow_ctx.nookal.documents if d.document_id == "doc_4001"
    ] or len(workflow_ctx.nookal.documents) == 1


def test_c6_missing_type(workflow_ctx: WorkflowTestContext) -> None:
    llm = FakeLLM(
        intent=FakeIntent(intent="request_certificate", confidence="high", extracted_fields={})
    )
    result = CertificateRequestWorkflow().run(
        workflow_ctx.orchestration_context(llm=llm),
        phone="+61411110001",
        message="need cert",
    )
    assert result.error and result.error.code == "missing_certificate_type"


def test_c6_bad_identity(workflow_ctx: WorkflowTestContext) -> None:
    llm = FakeLLM(
        intent=FakeIntent(
            intent="request_certificate",
            confidence="high",
            extracted_fields={"certificate_type": "attendance"},
        )
    )
    result = CertificateRequestWorkflow().run(
        workflow_ctx.orchestration_context(llm=llm),
        phone="+61000000000",
        message="cert",
    )
    assert result.error and result.error.code == "patient_not_found"


def test_c7_referral_letter_draft_task(workflow_ctx: WorkflowTestContext) -> None:
    llm = FakeLLM(
        draft=FakeDraft(text="Thank you for referring this patient.", missing_fields=[])
    )
    result = ReferralThankYouWorkflow().run(
        workflow_ctx.orchestration_context(correlation_id="c7-ok", llm=llm),
        referral_id="referral_3001",
    )
    assert result.status == WorkflowStatus.SUCCESS
    task = workflow_ctx.approval.get(result.data["task_id"])
    assert task.type == TaskType.LETTER
    assert task.status == TaskStatus.PENDING_REVIEW
    assert task.content_draft["status_tag"] == "DRAFT"
    # Audit must not contain draft body
    blob = " ".join(str(e.metadata) for e in workflow_ctx.audit.events)
    assert "Thank you for referring" not in blob


def test_c7_missing_fields_flagged(workflow_ctx: WorkflowTestContext) -> None:
    llm = FakeLLM(
        draft=FakeDraft(
            text="Dear [MISSING: referrer_name]",
            missing_fields=["referrer_name"],
        )
    )
    result = ReferralThankYouWorkflow().run(
        workflow_ctx.orchestration_context(llm=llm),
        referral_id="referral_3001",
    )
    assert result.data["missing_fields"] == ["referrer_name"]


def test_c7_llm_failure(workflow_ctx: WorkflowTestContext) -> None:
    llm = FakeLLM(fail_draft=True)
    result = ReferralThankYouWorkflow().run(
        workflow_ctx.orchestration_context(llm=llm),
        referral_id="referral_3001",
    )
    assert result.status == WorkflowStatus.FAILED
    assert result.error and result.error.code == "llm_failure"


def test_c8_exact_ambiguous_new(workflow_ctx: WorkflowTestContext) -> None:
    scenarios = sync_scenarios()
    store = ReferrerConflictStore()
    candidates = [
        scenarios["exact_match_candidate"],
        scenarios["ambiguous_candidate"],
        scenarios["new_referrer_candidate"],
    ]
    result = ReferrerSyncWorkflow().run(
        workflow_ctx.orchestration_context(correlation_id="c8-ok"),
        candidates=candidates,
        conflict_store=store,
    )
    assert result.status == WorkflowStatus.SUCCESS
    assert result.data["exact_updated"] == 1
    assert result.data["conflict"] == 1
    assert result.data["new_pending_count"] == 1
    assert store.conflicts and store.new_pending
    assert any(e.action == "referrer_sync.exact_updated" for e in workflow_ctx.audit.events)
    assert any(e.action == "referrer_sync.conflict" for e in workflow_ctx.audit.events)
    assert any(e.action == "referrer_sync.new_pending" for e in workflow_ctx.audit.events)
