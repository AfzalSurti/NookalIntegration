from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status

from app.dashboard.auth import User
from app.dashboard.authorization import Permission
from app.dashboard.dependencies import get_correlation_id, require_permission, review_service
from app.dashboard.schemas import DocumentResolveRequest, ExpenseConfirmRequest
from app.dashboard.services import ReviewService
from app.doc_intake import UploadedFile

router = APIRouter(prefix="/api/finance", tags=["finance"])


@router.post("/documents", status_code=status.HTTP_201_CREATED)
async def upload_document(
    file: Annotated[UploadFile, File()],
    source_channel: Annotated[str, Form()] = "upload",
    patient_name: Annotated[str | None, Form()] = None,
    user: Annotated[User, Depends(require_permission(Permission.DOCUMENT_REVIEW))] = None,  # type: ignore[assignment]
    correlation_id: Annotated[str, Depends(get_correlation_id)] = "",
    svc: Annotated[ReviewService, Depends(review_service)] = None,  # type: ignore[assignment]
) -> dict[str, object]:
    content = await file.read()
    record = svc.upload(
        uploaded=UploadedFile(file.filename or "upload", content, file.content_type),
        source_channel=source_channel, actor=user.user_id, patient_name=patient_name,
    )
    return {"document_id": record.document_id, "match_status": record.patient_match.status, "document_type": record.document_type}


@router.post("/documents/{document_id}/resolve")
def resolve_document(
    document_id: str,
    body: DocumentResolveRequest,
    user: Annotated[User, Depends(require_permission(Permission.DOCUMENT_REVIEW))],
    svc: Annotated[ReviewService, Depends(review_service)],
    correlation_id: Annotated[str, Depends(get_correlation_id)],
) -> dict[str, object]:
    try:
        record = svc.resolve_document(document_id, patient_id=body.patient_id, actor=user.user_id)
    except KeyError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="document_not_found") from None
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    return {"document_id": record.document_id, "filed_to": record.filed_to, "filing_status": record.filing_status}


@router.get("/expenses/review")
def list_expenses(
    user: Annotated[User, Depends(require_permission(Permission.EXPENSE_REVIEW))],
    svc: Annotated[ReviewService, Depends(review_service)],
) -> list[dict[str, object]]:
    return [{"document_id": item.document_id, "status": item.status, "confidence": item.extraction_confidence} for item in svc.list_expenses()]


@router.post("/expenses/{document_id}/confirm")
def confirm_expense(
    document_id: str,
    body: ExpenseConfirmRequest,
    user: Annotated[User, Depends(require_permission(Permission.EXPENSE_REVIEW))],
    svc: Annotated[ReviewService, Depends(review_service)],
    correlation_id: Annotated[str, Depends(get_correlation_id)],
) -> dict[str, object]:
    try:
        result = svc.confirm_expense(document_id, fields=body.fields, category=body.category, actor=user.user_id)
    except KeyError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="expense_not_found") from None
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    return {"document_id": result.expense.document_id, "category": result.category, "filed_location": result.filed_location}