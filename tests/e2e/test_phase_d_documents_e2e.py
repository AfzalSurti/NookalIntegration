"""Phase D document pipeline E2E — offline only."""
from __future__ import annotations

import ast

from app.approval import TaskStatus
from app.letters.delivery import InMemoryDocumentDelivery
from app.letters.handlers import DocumentApprovalHandler, register_document_handlers
from app.letters.models import DocumentStatus
from app.letters.store import InMemoryDocumentStore
from app.orchestration.certificates import CertificateRequestWorkflow
from app.orchestration.referral_letters import ReferralThankYouWorkflow
from app.orchestration.treatment_letters import TreatmentCompletionLetterWorkflow
from app.shared.exceptions import repo_root
from tests.helpers import WorkflowTestContext
from tests.helpers.fake_llm import FakeDraft, FakeIntent, FakeLLM


def _wire(ctx: WorkflowTestContext):
    store = InMemoryDocumentStore()
    delivery = InMemoryDocumentDelivery()
    handler = DocumentApprovalHandler(
        store=store,
        delivery=delivery,
        audit=ctx.audit,
        clock=ctx.clock,
        correlation_id="doc-e2e",
    )
    register_document_handlers(ctx.approval, handler)
    return store, delivery, handler


def test_e2e_docs_certificate_reject_and_approve(workflow_ctx: WorkflowTestContext) -> None:
    store, delivery, _ = _wire(workflow_ctx)
    llm = FakeLLM(
        intent=FakeIntent(
            "request_certificate", {"certificate_type": "attendance"}, "high"
        )
    )
    a = CertificateRequestWorkflow().run(
        workflow_ctx.orchestration_context(correlation_id="doc-a", llm=llm),
        phone="+61411110001",
        message="cert",
    )
    b = CertificateRequestWorkflow().run(
        workflow_ctx.orchestration_context(llm=llm),
        phone="+61411110001",
        message="cert2",
    )
    workflow_ctx.approval.reject(b.data["task_id"], reviewer_id="dr_het")
    assert store.find_by_task(b.data["task_id"]) is None

    workflow_ctx.approval.approve(a.data["task_id"], reviewer_id="dr_het")
    doc = store.find_by_task(a.data["task_id"])
    assert doc and doc.status == DocumentStatus.DELIVERED
    assert store.get_content(doc.document_id).startswith(b"%PDF")
    assert delivery.sent


def test_e2e_docs_letters(workflow_ctx: WorkflowTestContext) -> None:
    store, _, _ = _wire(workflow_ctx)
    thank = ReferralThankYouWorkflow().run(
        workflow_ctx.orchestration_context(
            correlation_id="doc-letter",
            llm=FakeLLM(draft=FakeDraft(text="Thanks for referring the patient.")),
        ),
        referral_id="referral_3001",
    )
    workflow_ctx.approval.approve(thank.data["task_id"], reviewer_id="dr_afzal")
    assert store.find_by_task(thank.data["task_id"])

    done = TreatmentCompletionLetterWorkflow().run(
        workflow_ctx.orchestration_context(
            llm=FakeLLM(draft=FakeDraft(text="Episode complete.")),
        ),
        patient_id="pat_1002",
        facts={"completion_date": "2026-09-05"},
    )
    workflow_ctx.approval.approve(done.data["task_id"], reviewer_id="dr_het")
    doc = store.find_by_task(done.data["task_id"])
    assert doc and doc.document_type.value == "treatment_completion"

    # Correlation present on document audits
    assert any(
        e.metadata.get("correlation_id") == "doc-e2e"
        for e in workflow_ctx.audit.events
        if e.action.startswith("document.")
    )


def test_e2e_docs_letters_package_no_openclaw() -> None:
    root = repo_root() / "app" / "letters"
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = (node.module or "").lower()
                assert "openclaw" not in mod
            if isinstance(node, ast.Import):
                for a in node.names:
                    assert "openclaw" not in a.name.lower()
