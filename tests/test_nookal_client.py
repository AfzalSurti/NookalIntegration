from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.nookal_client import MockNookalClient, PatientRef, Appointment
from app.shared.exceptions import KillSwitchActive, NookalNotFound
from app.shared.kill_switch import activate, deactivate


@pytest.fixture()
def mock_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MockNookalClient:
    monkeypatch.setenv("AUTOMATION_HOME", str(tmp_path))
    flag = tmp_path / "KILL_SWITCH"
    if flag.exists():
        flag.unlink()
    from app.shared import kill_switch as ks

    monkeypatch.setattr(ks, "switch_path", lambda path=None: flag)
    return MockNookalClient(audit=lambda *a, **kw: None)


def test_mock_read_write(mock_client: MockNookalClient) -> None:
    mock_client.seed_patient(PatientRef(patient_id="p1", phone="+61400000000"))
    assert mock_client.get_patient("p1").phone == "+61400000000"

    appt = mock_client.create_appointment(
        {"patient_id": "p1", "starts_at": datetime(2026, 9, 10, 10, 0, tzinfo=timezone.utc)}
    )
    updated = mock_client.update_appointment(appt.appointment_id, status="cancelled")
    assert updated.status == "cancelled"


def test_kill_switch_blocks_writes(mock_client: MockNookalClient, tmp_path: Path) -> None:
    flag = tmp_path / "KILL_SWITCH"
    activate(path=flag, reason="test")
    try:
        with pytest.raises(KillSwitchActive):
            mock_client.create_appointment(
                {
                    "patient_id": "p1",
                    "starts_at": datetime(2026, 9, 10, 10, 0, tzinfo=timezone.utc),
                }
            )
    finally:
        deactivate(path=flag)


def test_not_found(mock_client: MockNookalClient) -> None:
    with pytest.raises(NookalNotFound):
        mock_client.get_patient("missing")
