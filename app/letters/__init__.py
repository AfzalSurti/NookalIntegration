"""
Letter / certificate document pipeline.

Independent of Nookal, messaging, and OpenClaw. Downstream save/send happens
only via ApprovalQueue handlers after human approval.
"""
from __future__ import annotations

from app.letters.delivery import (
    DocumentDelivery,
    FileSystemDocumentDelivery,
    InMemoryDocumentDelivery,
)
from app.letters.handlers import DocumentApprovalHandler, register_document_handlers
from app.letters.models import DocumentRecord, DocumentStatus, DocumentType
from app.letters.renderer import DocumentRenderer, SimplePdfRenderer
from app.letters.store import (
    DocumentStore,
    FileSystemDocumentStore,
    InMemoryDocumentStore,
)
from app.letters.templates import TemplateSpec, TemplateStore, load_template
from app.letters.validators import DocumentValidationError, validate_for_render

__all__ = [
    "DocumentApprovalHandler",
    "DocumentDelivery",
    "DocumentRecord",
    "DocumentRenderer",
    "DocumentStatus",
    "DocumentStore",
    "DocumentType",
    "DocumentValidationError",
    "FileSystemDocumentDelivery",
    "FileSystemDocumentStore",
    "InMemoryDocumentDelivery",
    "InMemoryDocumentStore",
    "SimplePdfRenderer",
    "TemplateSpec",
    "TemplateStore",
    "load_template",
    "register_document_handlers",
    "validate_for_render",
]
