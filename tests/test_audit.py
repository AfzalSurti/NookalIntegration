from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from app.shared.audit import AuditLog
from app.shared.config import clear_settings_cache
from app.shared.exceptions import AuditRejected


@pytest.fixture()
def audit_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AuditLog:
    clear_settings_cache()
    monkeypatch.setenv("AUTOMATION_HOME", str(tmp_path))
    # settings.yaml still resolved from repo; only path overrides via AUTOMATION_HOME
    log = AuditLog(directory=tmp_path / "audit")
    return log


def test_log_and_query(audit_log: AuditLog) -> None:
    audit_log.log_event(
        actor="nookal_client",
        action="get_patient",
        target_type="patient_record",
        target_id="p_1",
        result="success",
        metadata={"attempt": 0},
    )
    rows = audit_log.query(actor="nookal_client")
    assert len(rows) == 1
    assert rows[0].target_id == "p_1"


def test_rejects_sensitive_metadata(audit_log: AuditLog) -> None:
    with pytest.raises(AuditRejected):
        audit_log.log_event(
            actor="messaging",
            action="send",
            target_type="message",
            target_id="k1",
            result="success",
            metadata={"body": "Hi Jane, your diagnosis is..."},
        )


def test_rejects_long_free_text(audit_log: AuditLog) -> None:
    with pytest.raises(AuditRejected):
        audit_log.log_event(
            actor="x",
            action="y",
            target_type="system",
            target_id="1",
            result="success",
            metadata={"note_id": "x" * 500},
        )


def test_query_by_date(audit_log: AuditLog) -> None:
    audit_log.log_event("a", "act", "system", "1", "success")
    today = date.today()
    assert audit_log.query(date_from=today, date_to=today)
