from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from app.case_tracking import CaseTracking
from app.nookal_client import Appointment, MockNookalClient, PatientRef


def _tracker(tmp_path: Path, completed_count: int, audit: object = None) -> CaseTracking:
    client = MockNookalClient(audit=lambda *args, **kwargs: None)
    client.seed_patient(PatientRef("p1"))
    for index in range(completed_count):
        client.seed_appointment(
            Appointment(
                f"appointment_{index}",
                "p1",
                datetime(2026, 1, index + 1, tzinfo=timezone.utc),
                status="completed",
            )
        )
    return CaseTracking(
        client,
        store_path=tmp_path / "cases.json",
        audit=audit if audit is not None else (lambda *args, **kwargs: None),
    )


def test_under_threshold_has_no_flag(tmp_path: Path) -> None:
    counter = _tracker(tmp_path, 4).refresh(case_id="case_1", patient_id="p1")

    assert counter.session_count == 4
    assert counter.flag_raised is False


def test_threshold_raises_flag_once(tmp_path: Path) -> None:
    events: list[tuple[tuple, dict]] = []
    tracker = _tracker(tmp_path, 5, audit=lambda *args, **kwargs: events.append((args, kwargs)))
    first = tracker.refresh(case_id="case_1", patient_id="p1")
    second = tracker.refresh(case_id="case_1", patient_id="p1")

    assert first.session_count == 5
    assert first.flag_raised is True
    assert second.flag_raised_at == first.flag_raised_at
    assert len([event for event in events if event[0][1] == "raise_session_flag"]) == 1


def test_acknowledge_flow_is_local_and_audited(tmp_path: Path) -> None:
    events: list[tuple[tuple, dict]] = []
    tracker = _tracker(tmp_path, 5, audit=lambda *args, **kwargs: events.append((args, kwargs)))
    tracker.refresh(case_id="case_1", patient_id="p1")

    acknowledged = tracker.acknowledge_flag("case_1", "practitioner_1")

    assert acknowledged.acknowledged_by == "practitioner_1"
    assert acknowledged.flag_raised is True
    assert tracker.get_case("case_1") == acknowledged
    assert any(event[0][1] == "acknowledge_session_flag" for event in events)