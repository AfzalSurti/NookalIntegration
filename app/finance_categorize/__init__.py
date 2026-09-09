"""Human-confirmed categorisation and approved expense-document filing."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from app.doc_filing import DriveAdapter
from app.finance_extract import ExpenseRecord
from app.shared import audit as audit_mod
from app.shared.config import get_settings


AuditFn = Callable[..., Any]


@dataclass(frozen=True)
class CategorizedExpense:
    expense: ExpenseRecord
    category: str
    filed_location: str
    categorized_by: str
    categorized_at: str


class FinanceCategorizer:
    def __init__(
        self,
        drive: DriveAdapter,
        *,
        categories: Iterable[str] | None = None,
        audit: AuditFn | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        configured = categories if categories is not None else (get_settings().raw.get("finance") or {}).get("categories", [])
        self._categories = frozenset(str(category).strip() for category in configured if str(category).strip())
        self._drive = drive
        self._audit = audit or audit_mod.log_event
        self._now = now or (lambda: datetime.now(timezone.utc))

    def categorize(
        self,
        expense: ExpenseRecord,
        *,
        category: str | None,
        categorized_by: str,
    ) -> CategorizedExpense:
        if expense.status != "extracted":
            raise ValueError("expense must be confirmed before categorization")
        chosen = (category or expense.category or "").strip()
        if not chosen or chosen not in self._categories:
            raise ValueError("category must be explicitly confirmed and present in configured categories")

        folder = f"expenses/{chosen}"
        content = Path(expense.source_path).read_bytes()
        location = self._drive.save_document(
            filename=Path(expense.source_path).name,
            content=content,
            content_type=_mime_for(Path(expense.source_path)),
            folder=folder,
        )
        timestamp = self._now().isoformat()
        self._audit(
            "finance_categorize", "categorize_expense", "document", expense.document_id, "success",
            metadata={"category": chosen, "target": "drive"},
        )
        return CategorizedExpense(
            expense=replace(expense, category=chosen),
            category=chosen,
            filed_location=location,
            categorized_by=categorized_by,
            categorized_at=timestamp,
        )


def _mime_for(path: Path) -> str:
    import mimetypes

    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


__all__ = ["CategorizedExpense", "FinanceCategorizer"]