"""
Deterministic PDF renderer — stdlib only, no network, no Nookal/messaging imports.
"""
from __future__ import annotations

import hashlib
import re
from abc import ABC, abstractmethod
from typing import Any

from app.letters.models import DocumentRecord
from app.letters.templates import TemplateSpec
from app.shared.exceptions import AutomationError


class RenderError(AutomationError):
    pass


class DocumentRenderer(ABC):
    @abstractmethod
    def render(self, *, text: str, metadata: dict[str, Any] | None = None) -> bytes:
        """Return PDF bytes for the given plain text."""


def _escape_pdf_text(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


class SimplePdfRenderer(DocumentRenderer):
    """
    Minimal single-page (or multi-line) PDF writer.
    Output is deterministic for identical input text.
    """

    def render(self, *, text: str, metadata: dict[str, Any] | None = None) -> bytes:
        if not text or not text.strip():
            raise RenderError("cannot render empty document text")
        # Normalize newlines for stable output.
        lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        # PDF content stream — Helvetica, 11pt, top-down lines.
        y = 800
        content_lines = ["BT", "/F1 11 Tf", "50 800 Td", "14 TL"]
        first = True
        for line in lines[:60]:  # hard cap to keep PDF simple
            safe = _escape_pdf_text(line[:120])
            if first:
                content_lines.append(f"({safe}) Tj")
                first = False
            else:
                content_lines.append("T*")
                content_lines.append(f"({safe}) Tj")
            y -= 14
        content_lines.append("ET")
        stream = "\n".join(content_lines).encode("latin-1", errors="replace")

        objects: list[bytes] = []
        objects.append(b"1 0 obj<< /Type /Catalog /Pages 2 0 R >>endobj\n")
        objects.append(b"2 0 obj<< /Type /Pages /Kids [3 0 R] /Count 1 >>endobj\n")
        objects.append(
            b"3 0 obj<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>endobj\n"
        )
        objects.append(
            f"4 0 obj<< /Length {len(stream)} >>stream\n".encode("ascii")
            + stream
            + b"\nendstream\nendobj\n"
        )
        objects.append(
            b"5 0 obj<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>endobj\n"
        )

        out = bytearray(b"%PDF-1.4\n")
        offsets = [0]
        for obj in objects:
            offsets.append(len(out))
            out.extend(obj)
        xref_pos = len(out)
        out.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
        out.extend(b"0000000000 65535 f \n")
        for off in offsets[1:]:
            out.extend(f"{off:010d} 00000 n \n".encode("ascii"))
        out.extend(
            f"trailer<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_pos}\n%%EOF\n".encode("ascii")
        )
        return bytes(out)


def content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def render_document_text(spec: TemplateSpec, record: DocumentRecord) -> str:
    values = dict(record.source_facts)
    if record.approved_body is not None:
        values.setdefault("body", record.approved_body)
    return spec.render(values)


# Guard: this module must not import nookal or messaging (enforced by tests).
_ = re  # keep import used
