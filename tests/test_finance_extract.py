from __future__ import annotations

from pathlib import Path

from app.approval import ApprovalQueue, TaskStatus, TaskType
from app.finance_extract import FinanceExtract
from app.llm_service import ExpenseExtractionResult


class FakeOcr:
    def extract_text(self, *, path: Path, mime_type: str) -> str:
        return path.read_text(encoding="utf-8")


class FakeLlm:
    def __init__(self, result: ExpenseExtractionResult) -> None:
        self.result = result
        self.received_text = ""

    def extract_expense(self, document_text: str) -> ExpenseExtractionResult:
        self.received_text = document_text
        return self.result


def _extractor(tmp_path: Path, llm: FakeLlm, audit: object = None) -> FinanceExtract:
    queue = ApprovalQueue(
        store_path=tmp_path / "tasks.jsonl",
        audit=lambda **kwargs: None,
    )
    return FinanceExtract(
        llm,
        FakeOcr(),
        queue,
        audit=audit if audit is not None else (lambda *args, **kwargs: None),
    )


def test_complete_extraction_returns_values_without_review(tmp_path: Path) -> None:
    receipt = tmp_path / "synthetic-receipt.pdf"
    receipt.write_text("Synthetic receipt OCR", encoding="utf-8")
    llm = FakeLlm(
        ExpenseExtractionResult(
            fields={
                "date": "2026-09-01",
                "supplier": "Synthetic Supplier",
                "amount": "110.00",
                "gst": "10.00",
                "description": "Synthetic service",
                "category": "professional_services",
                "payment_reference": "SYN-001",
                "missing_fields": [],
            },
            confidence=0.95,
        )
    )

    record = _extractor(tmp_path, llm).extract(
        document_id="doc_synthetic_1", path=receipt, mime_type="application/pdf"
    )

    assert record.status == "extracted"
    assert record.amount == "110.00"
    assert record.missing_fields == ()
    assert llm.received_text == "Synthetic receipt OCR"


def test_missing_fields_are_null_and_explicitly_marked(tmp_path: Path) -> None:
    receipt = tmp_path / "synthetic-receipt.pdf"
    receipt.write_text("Partial synthetic OCR", encoding="utf-8")
    llm = FakeLlm(
        ExpenseExtractionResult(
            fields={
                "date": "2026-09-01",
                "supplier": None,
                "amount": None,
                "gst": "[MISSING: GST]",
                "description": "Synthetic service",
                "category": None,
                "payment_reference": None,
                "missing_fields": ["supplier", "amount", "gst", "category", "payment_reference"],
            },
            confidence=0.9,
        )
    )

    record = _extractor(tmp_path, llm).extract(
        document_id="doc_synthetic_2", path=receipt, mime_type="application/pdf"
    )

    assert record.status == "extracted"
    assert record.supplier is None
    assert record.amount is None
    assert set(record.missing_fields) == {
        "supplier", "amount", "gst", "category", "payment_reference"
    }


def test_low_confidence_creates_existing_approval_task(tmp_path: Path) -> None:
    receipt = tmp_path / "synthetic-receipt.jpg"
    receipt.write_text("Unclear synthetic OCR", encoding="utf-8")
    llm = FakeLlm(ExpenseExtractionResult(fields={"amount": "50.00"}, confidence=0.4))
    extractor = _extractor(tmp_path, llm)

    record = extractor.extract(
        document_id="doc_synthetic_3", path=receipt, mime_type="image/jpeg"
    )

    assert record.status == "pending_review"
    assert record.review_task_id is not None
    queue = extractor._approval
    task = queue.get(record.review_task_id)
    assert task.type == TaskType.EXPENSE_EXTRACTION
    assert task.status == TaskStatus.PENDING_REVIEW
    assert task.content_draft["fields"]["amount"] == "50.00"


def test_audit_metadata_does_not_contain_financial_values(tmp_path: Path) -> None:
    events: list[tuple[tuple, dict]] = []
    receipt = tmp_path / "synthetic-receipt.pdf"
    receipt.write_text("Synthetic OCR", encoding="utf-8")
    llm = FakeLlm(ExpenseExtractionResult(fields={"amount": "999.99"}, confidence=0.95))
    record = _extractor(tmp_path, llm, audit=lambda *args, **kwargs: events.append((args, kwargs))).extract(
        document_id="doc_synthetic_4", path=receipt, mime_type="application/pdf"
    )

    assert record.status == "extracted"
    assert "999.99" not in str(events)
    assert "Synthetic Supplier" not in str(events)
    assert events[-1][1]["metadata"] == {"status": "extracted", "result": "extracted"}
