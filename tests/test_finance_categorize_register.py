from __future__ import annotations

from pathlib import Path
from threading import Thread

from app.finance_categorize import FinanceCategorizer
from app.finance_extract import ExpenseRecord
from app.finance_register import FinanceRegister


class FakeDrive:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bytes, str, str | None]] = []

    def save_document(self, *, filename: str, content: bytes, content_type: str, folder: str | None = None) -> str:
        self.calls.append((filename, content, content_type, folder))
        return f"drive://{folder}/{filename}"


def _expense(tmp_path: Path, *, status: str = "extracted") -> ExpenseRecord:
    source = tmp_path / "synthetic-receipt.pdf"
    source.write_bytes(b"synthetic receipt")
    return ExpenseRecord(
        document_id="doc_synthetic_1", source_path=str(source), date="2026-09-01",
        supplier="Synthetic Supplier", amount="110.00", gst="10.00",
        description="Synthetic service", category="professional_services", payment_reference="SYN-1",
        missing_fields=(), extraction_confidence=0.95, status=status, review_task_id=None,
        created_at="2026-09-01T00:00:00+00:00",
    )


def test_categorization_requires_configured_category_and_files_copy(tmp_path: Path) -> None:
    drive = FakeDrive()
    result = FinanceCategorizer(drive, categories=["professional_services"], audit=lambda *a, **k: None).categorize(
        _expense(tmp_path), category="professional_services", categorized_by="synthetic-staff"
    )

    assert result.category == "professional_services"
    assert result.filed_location == "drive://expenses/professional_services/synthetic-receipt.pdf"
    assert drive.calls == [("synthetic-receipt.pdf", b"synthetic receipt", "application/pdf", "expenses/professional_services")]


def test_categorization_rejects_unconfirmed_or_unknown_category(tmp_path: Path) -> None:
    categorizer = FinanceCategorizer(FakeDrive(), categories=["travel"])
    try:
        categorizer.categorize(_expense(tmp_path, status="pending_review"), category="travel", categorized_by="staff")
    except ValueError as exc:
        assert "confirmed" in str(exc)
    else:
        raise AssertionError("pending review expense was categorized")

    try:
        categorizer.categorize(_expense(tmp_path), category="office", categorized_by="staff")
    except ValueError as exc:
        assert "configured categories" in str(exc)
    else:
        raise AssertionError("unknown category was accepted")


def test_register_appends_once_and_can_find_by_source_document_id(tmp_path: Path) -> None:
    expense = _expense(tmp_path)
    register = FinanceRegister(tmp_path / "register.xlsx", audit=lambda *a, **k: None)

    first = register.append(expense, filed_location="drive://expenses/professional_services/synthetic-receipt.pdf")
    second = register.append(expense, filed_location="drive://different-location")
    found = register.find(expense.document_id)

    assert first["source_document_id"] == expense.document_id
    assert second == first
    assert found == first


def test_register_concurrent_appends_do_not_duplicate_or_corrupt(tmp_path: Path) -> None:
    expense = _expense(tmp_path)
    register = FinanceRegister(tmp_path / "register.xlsx", audit=lambda *a, **k: None)
    threads = [Thread(target=register.append, args=(expense,), kwargs={"filed_location": "drive://synthetic"}) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    found = register.find(expense.document_id)
    assert found is not None
    assert found["source_document_id"] == expense.document_id
