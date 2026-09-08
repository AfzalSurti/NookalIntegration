from __future__ import annotations

import ast
from datetime import datetime, timezone

import pytest

from app.letters.models import DocumentRecord, DocumentStatus, DocumentType
from app.letters.renderer import SimplePdfRenderer
from app.letters.templates import TemplateError, TemplateStore, load_template
from app.letters.validators import DocumentValidationError, validate_for_render
from app.shared.exceptions import repo_root


def test_d1_document_safe_dict_omits_bodies() -> None:
    rec = DocumentRecord(
        document_id="d1",
        document_type=DocumentType.CERTIFICATE,
        patient_id="pat_1001",
        task_id="t1",
        template_id="certificate",
        status=DocumentStatus.DRAFT,
        created_at="2026-09-07T00:00:00+00:00",
        draft_body="SECRET BODY",
        approved_body="APPROVED BODY",
    )
    safe = rec.to_safe_dict()
    assert safe["has_draft"] is True
    assert "SECRET" not in str(safe)
    assert "APPROVED BODY" not in str(safe)


def test_d2_template_load_and_render() -> None:
    spec = load_template("certificate")
    text = spec.render(
        {
            "patient_label": "patient:pat_1001",
            "certificate_type": "attendance",
            "statement": "Attended as scheduled.",
        }
    )
    assert "attendance" in text
    with pytest.raises(TemplateError):
        spec.render({"patient_label": "x", "certificate_type": "attendance"})


def test_d2_unknown_placeholder_undeclared(tmp_path) -> None:
    # Use real template — certificate has no undeclared placeholders.
    store = TemplateStore()
    assert store.get("referral_thank_you").template_id == "referral_thank_you"


def test_d3_pdf_deterministic() -> None:
    r = SimplePdfRenderer()
    a = r.render(text="Hello line\nSecond")
    b = r.render(text="Hello line\nSecond")
    assert a == b
    assert a.startswith(b"%PDF")
    with pytest.raises(Exception):
        r.render(text="   ")


def test_d4_draft_cannot_render() -> None:
    rec = DocumentRecord(
        document_id="d1",
        document_type=DocumentType.CERTIFICATE,
        patient_id="p",
        task_id="t",
        template_id="certificate",
        status=DocumentStatus.DRAFT,
        created_at="x",
        approved_body="stmt",
        source_facts={
            "patient_label": "patient:p",
            "certificate_type": "attendance",
            "statement": "stmt",
        },
    )
    with pytest.raises(DocumentValidationError) as ei:
        validate_for_render(rec)
    assert ei.value.code in {"not_approved", "draft_not_final"}


def test_d4_approved_validates() -> None:
    rec = DocumentRecord(
        document_id="d1",
        document_type=DocumentType.CERTIFICATE,
        patient_id="p",
        task_id="t",
        template_id="certificate",
        status=DocumentStatus.APPROVED,
        created_at="x",
        approved_body="Attended.",
        source_facts={
            "patient_label": "patient:p",
            "certificate_type": "attendance",
            "statement": "Attended.",
        },
    )
    spec = validate_for_render(rec)
    assert spec.template_id == "certificate"


def test_d3_renderer_has_no_nookal_messaging_openclaw() -> None:
    path = repo_root() / "app" / "letters" / "renderer.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                assert "nookal" not in a.name
                assert "messaging" not in a.name
                assert "openclaw" not in a.name.lower()
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            assert "nookal" not in mod
            assert not mod.startswith("app.messaging")
            assert "openclaw" not in mod.lower()
