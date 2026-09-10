"""
Nookal Referral Report Parser.

Validates and extracts patient-level referral rows from Nookal CSV or delimited text exports.
Rejects aggregate reports (e.g. summaries containing only referrer names and referral counts).
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterable, Iterator, TextIO

from app.shared.exceptions import AutomationError


class MalformedReportError(AutomationError):
    """Raised when an exported report cannot be parsed or lacks patient-level rows."""


@dataclass(frozen=True)
class ReferralReportRow:
    """A single patient-level referral record extracted from a Nookal export."""

    patient_id: str | None
    patient_name: str | None
    referrer_name: str
    referrer_id: str | None = None
    provider_number: str | None = None
    case_id: str | None = None
    referral_date: str | None = None
    raw_row: dict[str, str] = None  # type: ignore


# Standard header alias mapping for patient-level exports
_PATIENT_ID_HEADERS = (
    "patient id",
    "patientid",
    "patient_id",
    "client id",
    "clientid",
    "client_id",
    "id",
)

_PATIENT_NAME_HEADERS = (
    "patient",
    "patient name",
    "patient_name",
    "client",
    "client name",
    "client_name",
    "name",
)

_REFERRER_NAME_HEADERS = (
    "referrer",
    "referring doctor",
    "referring_doctor",
    "referrer name",
    "referrer_name",
    "doctor",
    "doctor name",
    "source",
    "referral source",
)

_REFERRER_ID_HEADERS = (
    "referrer id",
    "referrerid",
    "referrer_id",
    "ref id",
    "ref_id",
)

_PROVIDER_NUMBER_HEADERS = (
    "provider number",
    "provider_number",
    "provider #",
    "provider no",
)

_CASE_ID_HEADERS = (
    "case",
    "case id",
    "case_id",
    "case number",
    "case_number",
)

_DATE_HEADERS = (
    "date",
    "referral date",
    "referral_date",
    "date referred",
    "created",
    "date created",
)


def _normalize_header(h: str) -> str:
    return re.sub(r"[\s_-]+", " ", h.strip().casefold())


def _find_column(fieldnames: Iterable[str], candidates: tuple[str, ...]) -> str | None:
    norm_map = {_normalize_header(f): f for f in fieldnames if f}
    for c in candidates:
        if c in norm_map:
            return norm_map[c]
    # Partial prefix match
    for norm, orig in norm_map.items():
        for c in candidates:
            if norm.startswith(c):
                return orig
    return None


class ReferralReportParser:
    """Parses and validates Nookal referral export files."""

    @classmethod
    def parse_file(cls, path: Path | str) -> list[ReferralReportRow]:
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"Report file not found: {p}")
        raw_bytes = p.read_bytes()
        return cls.parse_bytes(raw_bytes, source_name=p.name)

    @classmethod
    def parse_bytes(cls, content: bytes, source_name: str = "report.csv") -> list[ReferralReportRow]:
        # Handle UTF-8 with BOM or fallback to latin-1
        text = ""
        for enc in ("utf-8-sig", "utf-8", "latin-1"):
            try:
                text = content.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if not text:
            raise MalformedReportError("Unable to decode report file content as text.")
        return cls.parse_text(text, source_name=source_name)

    @classmethod
    def parse_text(cls, text: str, source_name: str = "report.csv") -> list[ReferralReportRow]:
        cleaned = text.strip()
        if not cleaned:
            return []

        # Sniff delimiter (comma, tab, semicolon)
        sample = "\n".join(cleaned.splitlines()[:5])
        delimiter = ","
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",\t;")
            delimiter = dialect.delimiter
        except Exception:
            delimiter = "\t" if "\t" in sample else ","

        reader = csv.DictReader(io.StringIO(cleaned), delimiter=delimiter)
        if not reader.fieldnames:
            raise MalformedReportError("Report file contains no readable headers.")

        fieldnames = [f.strip() for f in reader.fieldnames if f]

        col_pid = _find_column(fieldnames, _PATIENT_ID_HEADERS)
        col_pname = _find_column(fieldnames, _PATIENT_NAME_HEADERS)
        col_ref = _find_column(fieldnames, _REFERRER_NAME_HEADERS)
        col_ref_id = _find_column(fieldnames, _REFERRER_ID_HEADERS)
        col_prov = _find_column(fieldnames, _PROVIDER_NUMBER_HEADERS)
        col_case = _find_column(fieldnames, _CASE_ID_HEADERS)
        col_date = _find_column(fieldnames, _DATE_HEADERS)

        # A patient-level report MUST identify referrers and have at least a patient ID or patient name
        if not col_ref:
            raise MalformedReportError(
                f"Missing required referrer column in report. Headers found: {fieldnames}"
            )
        if not col_pid and not col_pname:
            # Check if this is an aggregate marketing summary
            aggregate_keywords = {"total", "count", "cases count", "clients count", "revenue"}
            has_aggregate = any(any(ak in _normalize_header(f) for ak in aggregate_keywords) for f in fieldnames)
            if has_aggregate:
                raise MalformedReportError(
                    "Report appears to be an aggregate summary, not a patient-level report. "
                    "Patient-level data (Patient ID or Patient Name) is required."
                )
            raise MalformedReportError(
                f"Missing required patient identifier or patient name column. Headers found: {fieldnames}"
            )

        rows: list[ReferralReportRow] = []
        for raw in reader:
            if not raw or not any(v.strip() for v in raw.values() if v):
                continue

            ref_val = (raw.get(col_ref) or "").strip()
            if not ref_val:
                # Skip rows where referrer is empty
                continue

            pid_val = (raw.get(col_pid) or "").strip() if col_pid else None
            pname_val = (raw.get(col_pname) or "").strip() if col_pname else None
            ref_id_val = (raw.get(col_ref_id) or "").strip() if col_ref_id else None
            prov_val = (raw.get(col_prov) or "").strip() if col_prov else None
            case_val = (raw.get(col_case) or "").strip() if col_case else None
            date_val = (raw.get(col_date) or "").strip() if col_date else None

            rows.append(
                ReferralReportRow(
                    patient_id=pid_val or None,
                    patient_name=pname_val or None,
                    referrer_name=ref_val,
                    referrer_id=ref_id_val or None,
                    provider_number=prov_val or None,
                    case_id=case_val or None,
                    referral_date=date_val or None,
                    raw_row=dict(raw),
                )
            )

        return rows
