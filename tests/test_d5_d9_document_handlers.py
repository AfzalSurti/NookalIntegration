from __future__ import annotations

from app.approval import TaskStatus, TaskType
from app.letters.delivery import InMemoryDocumentDelivery
from app.letters.handlers import DocumentApprovalHandler, register_document_handlers
from app.letters.models import DocumentStatus
from app.letters.store import InMemoryDocumentStore
from app.orchestration.certificates import CertificateRequestWorkflow
from app.orchestration.referral_letters import ReferralThankYouWorkflow
from app.orchestration.results import WorkflowStatus
from app.orchestration.treatment_letters import TreatmentCompletionLetterWorkflow
from app.shared.kill_switch import activate, deactivate
from tests.helpers import WorkflowTestContext
from tests.helpers.fake_llm import FakeDraft, FakeIntent, FakeLLM


def test_d5_d6_certificate_approve_renders(
    workflow_ctx: WorkflowTestContext, monkeypatch
) -> None:
    store = InMemoryDocumentStore()
    delivery = InMemoryDocumentDelivery()
    handler = DocumentApprovalHandler(
        store=store,
        delivery=delivery,
        audit=workflow_ctx.audit,
        clock=workflow_ctx.clock,
        correlation_id="d6-corr",
    )
    register_document_handlers(workflow_ctx.approval, handler)

    llm = FakeLLM(
        intent=FakeIntent(
            intent="request_certificate",
            confidence="high",
            extracted_fields={"certificate_type": "attendance"},
        )
    )
    created = CertificateRequestWorkflow().run(
        workflow_ctx.orchestration_context(correlation_id="d6-1", llm=llm),
        phone="+61411110001",
        message="need cert",
    )
    assert created.status == WorkflowStatus.SUCCESS
    task_id = created.data["task_id"]
    task = workflow_ctx.approval.get(task_id)
    assert task.status == TaskStatus.PENDING_REVIEW

    # Reject path: no document
    other = CertificateRequestWorkflow().run(
        workflow_ctx.orchestration_context(
            llm=FakeLLM(
                intent=FakeIntent(
                    "request_certificate",
                    {"certificate_type": "attendance"},
                    "high",
                )
            )
        ),
        phone="+61411110001",
        message="cert2",
    )
    workflow_ctx.approval.reject(other.data["task_id"], reviewer_id="dr_het", notes="no")
    assert store.find_by_task(other.data["task_id"]) is None

    approved = workflow_ctx.approval.approve(task_id, reviewer_id="dr_het", notes="ok")
    assert approved.status == TaskStatus.SENT
    doc = store.find_by_task(task_id)
    assert doc is not None
    assert doc.status == DocumentStatus.DELIVERED
    assert store.get_content(doc.document_id).startswith(b"%PDF")
    assert len(delivery.sent) == 1
    assert not any("Attended" in str(e.metadata) for e in workflow_ctx.audit.events)

    # Duplicate handler invocation does not double-deliver
    handler.handle(approved)
    assert len(delivery.sent) == 1


def test_d7_referral_approve_pdf(workflow_ctx: WorkflowTestContext) -> None:
    store = InMemoryDocumentStore()
    delivery = InMemoryDocumentDelivery()
    register_document_handlers(
        workflow_ctx.approval,
        DocumentApprovalHandler(
            store=store, delivery=delivery, audit=workflow_ctx.audit, clock=workflow_ctx.clock
        ),
    )
    result = ReferralThankYouWorkflow().run(
        workflow_ctx.orchestration_context(
            llm=FakeLLM(draft=FakeDraft(text="Thank you for this referral."))
        ),
        referral_id="referral_3001",
    )
    task_id = result.data["task_id"]
    workflow_ctx.approval.approve(task_id, reviewer_id="dr_afzal")
    doc = store.find_by_task(task_id)
    assert doc is not None
    pdf = store.get_content(doc.document_id)
    assert pdf and pdf.startswith(b"%PDF")
    assert "Thank you for this referral" not in str(
        [e.metadata for e in workflow_ctx.audit.events]
    )


def test_d8_treatment_completion(workflow_ctx: WorkflowTestContext) -> None:
    store = InMemoryDocumentStore()
    register_document_handlers(
        workflow_ctx.approval,
        DocumentApprovalHandler(
            store=store,
            delivery=InMemoryDocumentDelivery(),
            audit=workflow_ctx.audit,
            clock=workflow_ctx.clock,
        ),
    )
    result = TreatmentCompletionLetterWorkflow().run(
        workflow_ctx.orchestration_context(
            llm=FakeLLM(draft=FakeDraft(text="Treatment episode concluded as planned."))
        ),
        patient_id="pat_1001",
        facts={"completion_date": "2026-09-01"},
    )
    assert result.status == WorkflowStatus.SUCCESS
    workflow_ctx.approval.approve(result.data["task_id"], reviewer_id="dr_het")
    doc = store.find_by_task(result.data["task_id"])
    assert doc is not None
    assert doc.document_type.value == "treatment_completion"


def test_d9_kill_switch_blocks_delivery(workflow_ctx: WorkflowTestContext, monkeypatch) -> None:
    from app.shared import kill_switch as ks

    flag = workflow_ctx.kill_switch_path
    monkeypatch.setattr(ks, "switch_path", lambda path=None: flag)
    store = InMemoryDocumentStore()
    delivery = InMemoryDocumentDelivery()
    register_document_handlers(
        workflow_ctx.approval,
        DocumentApprovalHandler(
            store=store, delivery=delivery, audit=workflow_ctx.audit, clock=workflow_ctx.clock
        ),
    )
    created = CertificateRequestWorkflow().run(
        workflow_ctx.orchestration_context(
            llm=FakeLLM(
                intent=FakeIntent(
                    "request_certificate",
                    {"certificate_type": "attendance"},
                    "high",
                )
            )
        ),
        phone="+61411110001",
        message="cert",
    )
    activate(path=flag, reason="test")
    try:
        with __import__("pytest").raises(Exception):
            workflow_ctx.approval.approve(
                created.data["task_id"], reviewer_id="dr_het"
            )
        task = workflow_ctx.approval.get(created.data["task_id"])
        assert task.status == TaskStatus.FAILED
        assert delivery.sent == []
    finally:
        deactivate(path=flag)
