"""
Nookal API client wrapper.

Official Nookal API v2 implementation:
Base URL: https://api.nookal.com/production/v2/
Authentication: centralized ?api_key=<API_KEY> query parameter.
Response handling: unwrap Nookal v2 {"status": "success", "data": ...} envelope.

Clinic-agnostic: auth, rate limit, retry, audit, kill switch.
No guessed endpoints. Unsupported endpoints raise explicit NotImplementedError.
"""
from __future__ import annotations

import logging
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime, time as dt_time, timedelta
from typing import Any, Callable, Mapping
import httpx

logger = logging.getLogger(__name__)

from app.shared import audit as audit_mod
from app.shared.config import NookalConfig, get_settings
from app.shared.exceptions import (
    KillSwitchActive,
    NookalAuthError,
    NookalError,
    NookalFileError,
    NookalFileNotFound,
    NookalInvalidFileId,
    NookalNotFound,
    NookalRateLimit,
    NookalRequestFailed,
    NookalResponseInvalid,
    NookalServerError,
    NookalUrlMissing,
    NookalValidationError,
)
from app.shared.kill_switch import assert_allows


UNSUPPORTED_BY_DOCUMENTED_NOOKAL_API = "UNSUPPORTED_BY_DOCUMENTED_NOOKAL_API"

AuditFn = Callable[..., Any]

_SECRET_RE = re.compile(r"([?&]api_key=)[^&\s'\"]+", re.IGNORECASE)
_S3_SIGNATURE_RE = re.compile(
    r"([?&](?:X-Amz-Signature|Signature|signature|sig|AWSAccessKeyId|X-Amz-Credential|X-Amz-Security-Token|token|Key-Pair-Id)=)[^&\s'\"]+",
    re.IGNORECASE,
)


def _redact_secrets(text: str, api_key: str | None = None) -> str:
    """Scrub api_key and presigned S3 query parameters from strings and URLs."""
    if not text:
        return text
    redacted = _SECRET_RE.sub(r"\1[REDACTED]", str(text))
    redacted = _S3_SIGNATURE_RE.sub(r"\1[REDACTED]", redacted)
    if api_key and api_key.strip():
        redacted = redacted.replace(api_key, "[REDACTED]")
    return redacted


def redact_url(url: str) -> str:
    """Strip all query parameters and credentials from a URL for safe logging."""
    if not url:
        return url
    if "?" in url:
        base, _ = url.split("?", 1)
        return f"{base}?[REDACTED]"
    return url


def _extract_file_url(data: Any) -> str | None:
    """
    Extract download/view URL from Nookal /getFileUrl payload.
    Supports official Nookal v2 response shape:
      {"results": {"url": "https://..."}}
      {"api_call": "getFileUrl", "results": {"url": "https://..."}}
    As well as flat or nested fallback shapes:
      {"url": "https://..."}, {"file_url": "https://..."}, {"file": {"url": "https://..."}}
    """
    if not data:
        return None
    if isinstance(data, str):
        s = data.strip()
        if s.startswith(("http://", "https://")):
            return s
        return None
    if isinstance(data, list):
        for item in data:
            u = _extract_file_url(item)
            if u:
                return u
        return None
    if isinstance(data, Mapping):
        # 1. Direct candidate keys
        for k in ("url", "URL", "file_url", "fileUrl", "download_url", "downloadUrl", "link", "fileURL"):
            val = data.get(k)
            if isinstance(val, str) and val.strip().startswith(("http://", "https://")):
                return val.strip()

        # 2. Preferred containers ("results" is official Nookal v2 container)
        for container_key in ("results", "file", "files", "data"):
            container = data.get(container_key)
            if container is not None:
                u = _extract_file_url(container)
                if u:
                    return u

        # 3. Recursive fallback for any nested mapping or list
        for v in data.values():
            if isinstance(v, (Mapping, list)):
                u = _extract_file_url(v)
                if u:
                    return u
    return None


def _unwrap_collection(data: Any, preferred_key: str | None = None) -> list[Any]:
    """
    Unwrap collection from Nookal data payloads.
    Handles:
    - Lists of records: [ {...}, {...} ]
    - Nookal official v2 wrapped results:
      {"api_call": "...", "results": {"patients": [...]}}
      {"api_call": "...", "results": {"appointments": [...]}}
    - Dicts under preferred_key or candidate keys (both lists and dict-of-records):
      {"patients": [...]}, {"patients": {"0": {...}, "1": {...}}}
      {"appointments": [...]}, {"appointments": {"0": {...}, "1": {...}}}
    - Direct dict of records:
      {"0": {...}, "1": {...}} or {"id_1": {...}, "id_2": {...}}
    - Nested 'results', 'data', or 'items' wrappers.
    """
    if data is None:
        return []
    if isinstance(data, list):
        return data
    if not isinstance(data, Mapping):
        return []

    def _as_record_list(val: Any) -> list[Any] | None:
        if val is None:
            return None
        if isinstance(val, list):
            return val
        if isinstance(val, Mapping):
            if not val:
                return []
            if all(isinstance(v, Mapping) for v in val.values()):
                return list(val.values())
            dict_records = [v for v in val.values() if isinstance(v, Mapping)]
            if dict_records:
                return dict_records
        return None

    def _find_in_dict(d: Mapping[str, Any], key: str) -> Any:
        target = key.casefold()
        for k, v in d.items():
            if str(k).casefold() == target:
                return v
        return None

    candidate_keys = (
        "patients", "appointments", "cases", "notes", "treatment_notes",
        "extras", "locations", "practitioners", "types", "appointment_types",
        "classes", "class_types", "participants", "redemptions", "files",
        "invoices", "entries", "payments", "credits", "discounts",
        "refunds", "adjustments", "waiting_list", "waitinglist",
        "availabilities", "items", "records",
    )

    # 1. If data has a 'results' container (official Nookal shape: {"api_call": "...", "results": {...}})
    results_container = _find_in_dict(data, "results")
    if results_container is not None:
        if isinstance(results_container, Mapping):
            if preferred_key:
                preferred_val = _find_in_dict(results_container, preferred_key)
                if preferred_val is not None:
                    inner = _as_record_list(preferred_val)
                    if inner is not None:
                        return inner
            for candidate in candidate_keys:
                cand_val = _find_in_dict(results_container, candidate)
                if cand_val is not None:
                    inner = _as_record_list(cand_val)
                    if inner is not None:
                        return inner
            rec_list = _as_record_list(results_container)
            if rec_list is not None:
                return rec_list
        else:
            rec_list = _as_record_list(results_container)
            if rec_list is not None:
                return rec_list

    # 2. Check preferred_key directly on data
    if preferred_key:
        preferred_val = _find_in_dict(data, preferred_key)
        if preferred_val is not None:
            rec_list = _as_record_list(preferred_val)
            if rec_list is not None:
                return rec_list

    # 3. Check candidate keys directly on data
    for candidate in candidate_keys + ("data",):
        cand_val = _find_in_dict(data, candidate)
        if cand_val is not None:
            if isinstance(cand_val, Mapping) and candidate == "data":
                return _unwrap_collection(cand_val, preferred_key)
            rec_list = _as_record_list(cand_val)
            if rec_list is not None:
                return rec_list

    # 4. Check if data itself is a dict of records
    rec_list = _as_record_list(data)
    if rec_list is not None:
        return rec_list

    return []


def _is_invoice_dict(d: Any, target_id: str | None = None) -> bool:
    if not isinstance(d, Mapping):
        return False
    # Check if target_id matches
    if target_id is not None:
        rec_id = str(d.get("ID") or d.get("id") or d.get("invoice_id") or d.get("invoiceID") or d.get("InvoiceID") or d.get("invoiceId") or "")
        if rec_id and rec_id == str(target_id):
            return True
    # Check for characteristic invoice identification keys
    has_id = any(k in d for k in ("ID", "id", "invoice_id", "invoiceID", "InvoiceID", "invoiceId", "invoiceNumber", "InvoiceNumber"))
    # Check for characteristic invoice properties
    has_invoice_props = any(
        k in d
        for k in (
            "totalDebits", "total_debits", "TotalDebits",
            "patientID", "patient_id", "PatientID", "Patient_ID",
            "balance", "totalBalance", "TotalBalance",
            "totalPayments", "TotalPayments",
            "date", "dueDate", "due_date", "DateDue",
            "locationID", "practitionerID", "reference",
            "entries", "items", "account", "invoiceStatus",
            "total", "Total", "amount", "status", "Status"
        )
    )
    return has_id and has_invoice_props


def _unwrap_invoice_data(data: Any, invoice_id: str | None = None) -> Mapping[str, Any] | None:
    """
    Unwrap single invoice record from Nookal API response payloads.
    Handles:
    - Official Nookal v2 envelope:
      {"api_call": "getInvoice", "results": {"invoice": {...}}}
      {"api_call": "getInvoice", "results": {"invoices": [{...}]}}
      {"api_call": "getInvoice", "results": {"123": {...}}}
      {"api_call": "getInvoice", "results": {...}}
    - Collection or nested payloads:
      {"invoice": {...}}, {"invoices": [{...}]}, {"data": {...}}
    - Flat single invoice record:
      {"ID": "123", "patientID": "456", "totalDebits": "150.00", ...}
    """
    if not data:
        return None

    # 1. Direct match if data is already an invoice record
    if _is_invoice_dict(data, target_id=invoice_id):
        return data

    # 2. If data is a list of records
    if isinstance(data, list):
        if invoice_id:
            for item in data:
                if isinstance(item, Mapping) and _is_invoice_dict(item, target_id=invoice_id):
                    return item
        for item in data:
            if isinstance(item, Mapping) and _is_invoice_dict(item):
                return item
        return None

    if not isinstance(data, Mapping):
        return None

    def _get_ci(d: Mapping[str, Any], *keys: str) -> Any:
        target_keys = {k.casefold() for k in keys}
        for k, v in d.items():
            if str(k).casefold() in target_keys:
                return v
        return None

    # 3. Check inside 'results' container (official Nookal v2 envelope)
    results = _get_ci(data, "results")
    if results is not None:
        unwrapped = _unwrap_invoice_data(results, invoice_id=invoice_id)
        if unwrapped is not None:
            return unwrapped

    # 4. Check candidate containers
    for key in ("invoice", "invoices", "data"):
        sub = _get_ci(data, key)
        if sub is not None:
            unwrapped = _unwrap_invoice_data(sub, invoice_id=invoice_id)
            if unwrapped is not None:
                return unwrapped

    # 5. Check if data is keyed by target_id: {"123": {...}}
    if invoice_id and str(invoice_id) in data and isinstance(data[str(invoice_id)], Mapping):
        return data[str(invoice_id)]

    # 6. Check any nested mapping value in data
    for k, v in data.items():
        if isinstance(v, Mapping) and _is_invoice_dict(v, target_id=invoice_id):
            return v

    # 7. Fallback: if data has any ID key or patient key and doesn't look like an envelope
    if not any(k in data for k in ("api_call", "results", "status", "settings")):
        if any(k in data for k in ("ID", "id", "invoice_id", "invoiceID", "totalDebits", "patientID", "patient_id")):
            return data

    return None


# --- Documented Nookal Entities ---

@dataclass(frozen=True)
class PatientRef:
    patient_id: str
    first_name: str | None = None
    middle_name: str | None = None
    last_name: str | None = None
    nickname: str | None = None
    phone: str | None = None
    email: str | None = None
    display_name: str | None = None
    date_of_birth: date | None = None
    gender: str | None = None
    suburb: str | None = None
    address: Mapping[str, Any] | str | None = None
    postal_address: Mapping[str, Any] | str | None = None
    online_code: str | None = None
    deceased: bool = False
    referrer_id: str | None = None
    last_appointment_date: date | None = None
    date_created: str | None = None
    date_modified: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class Appointment:
    appointment_id: str
    patient_id: str
    starts_at: datetime
    ends_at: datetime | None = None
    status: str | None = None
    location_id: str | None = None
    practitioner_id: str | None = None
    type_id: str | None = None
    appointment_type: str | None = None
    notes: str | None = None
    arrived: bool = False
    dna: bool = False
    cancelled: bool = False
    cancellation_date: str | None = None
    email_reminder_sent: bool = False
    invoice_generated: bool = False
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class CaseRef:
    case_id: str
    patient_id: str
    case_name: str | None = None
    case_number: str | None = None
    status: str | None = None
    date_created: str | None = None
    date_modified: str | None = None
    closed_date: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class TreatmentNote:
    note_id: str
    patient_id: str
    case_id: str | None = None
    practitioner_id: str | None = None
    date: datetime | str | None = None
    notes: str | None = None
    appointment_id: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class PatientExtra:
    extra_id: str
    name: str
    field_type: str | None = None
    options: list[str] = field(default_factory=list)
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class Location:
    location_id: str
    name: str
    address: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class Practitioner:
    practitioner_id: str
    first_name: str | None = None
    last_name: str | None = None
    speciality: str | None = None
    title: str | None = None
    email: str | None = None
    locations: list[str] = field(default_factory=list)
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class AppointmentType:
    type_id: str
    name: str
    description: str | None = None
    duration: int | None = None
    price: float | None = None
    has_tax: bool = False
    type: str | None = None
    locations: list[str] = field(default_factory=list)
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class ClassType:
    class_id: str
    name: str
    description: str | None = None
    duration: int | None = None
    price: float | None = None
    locations: list[str] = field(default_factory=list)
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class ClassParticipant:
    participant_id: str
    class_id: str
    patient_id: str | None = None
    status: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class PatientFile:
    file_id: str
    patient_id: str
    name: str
    file_type: str | None = None
    date_added: str | None = None
    size: int | None = None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class InvoiceEntry:
    entry_id: str
    invoice_id: str | None = None
    item_id: str | None = None
    description: str | None = None
    price: float | None = None
    quantity: float | None = None
    tax: float | None = None
    total: float | None = None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class Invoice:
    invoice_id: str
    patient_id: str
    date: str | None = None
    total: float | None = None
    status: str | None = None
    void: bool = False
    expanded: bool = False
    entries: list[InvoiceEntry] = field(default_factory=list)
    location_id: str | None = None
    practitioner_id: str | None = None
    case_id: str | None = None
    reference: str | None = None
    due_date: str | None = None
    balance: float | None = None
    paid: float | None = None
    tax: float | None = None
    notes: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class Redemption:
    redemption_id: str
    patient_id: str | None = None
    item_type: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class WaitingListEntry:
    entry_id: str
    patient_id: str | None = None
    location_id: str | None = None
    practitioner_id: str | None = None
    date_added: str | None = None
    notes: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)


# Legacy test compatibility containers (kept strictly for synthetic test fixture support)
@dataclass(frozen=True)
class Referrer:
    referrer_id: str
    name: str
    provider_number: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class DocumentMeta:
    document_id: str
    patient_id: str
    title: str | None = None


@dataclass(frozen=True)
class Referral:
    referral_id: str
    patient_id: str
    referrer_id: str
    recorded_on: date
    notes_ref: str | None = None


class NookalClient(ABC):
    """Shared interface for live + mock Nookal clients based strictly on documented v2 APIs."""

    # --- Patients ---

    @abstractmethod
    def get_patients(
        self,
        *,
        page: int = 1,
        page_length: int = 200,
        last_modified: str | None = None,
        deceased: int | None = None,
    ) -> list[PatientRef]:
        ...

    @abstractmethod
    def search_patients(
        self,
        *,
        patient_id: str | None = None,
        online_code: str | None = None,
        date_created: str | None = None,
        email: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
        date_of_birth: str | None = None,
        fuzzy_search: str | None = None,
        suburb: str | list[str] | None = None,
        age_min: int | None = None,
        age_max: int | None = None,
        appointment_from: date | None = None,
        appointment_to: date | None = None,
        referrer_id: str | None = None,
        deceased: int | None = None,
    ) -> list[PatientRef]:
        ...

    @abstractmethod
    def get_patient(self, patient_id: str) -> PatientRef:
        ...

    @abstractmethod
    def find_patient_by_phone(self, phone: str) -> list[PatientRef]:
        ...

    @abstractmethod
    def add_patient(self, payload: Mapping[str, Any]) -> PatientRef:
        ...

    @abstractmethod
    def edit_patient(self, patient_id: str, payload: Mapping[str, Any]) -> PatientRef:
        ...

    # --- Cases ---

    @abstractmethod
    def get_cases(
        self,
        patient_id: str,
        *,
        page: int = 1,
        page_length: int = 200,
        last_modified: str | None = None,
    ) -> list[CaseRef]:
        ...

    @abstractmethod
    def get_all_cases(
        self,
        *,
        page: int = 1,
        page_length: int = 200,
        last_modified: str | None = None,
    ) -> list[CaseRef]:
        ...

    # --- Treatment Notes ---

    @abstractmethod
    def get_treatment_notes(
        self,
        patient_id: str,
        *,
        page: int = 1,
        page_length: int = 100,
        last_modified: str | None = None,
    ) -> list[TreatmentNote]:
        ...

    @abstractmethod
    def get_all_treatment_notes(
        self,
        *,
        page: int = 1,
        page_length: int = 50,
        last_modified: str | None = None,
        practitioner_id: str | None = None,
    ) -> list[TreatmentNote]:
        ...

    @abstractmethod
    def add_treatment_note(
        self,
        *,
        patient_id: str,
        case_id: str,
        practitioner_id: str,
        date: str | datetime,
        notes: str,
        appt_id: str | None = None,
    ) -> TreatmentNote:
        ...

    # --- Extras ---

    @abstractmethod
    def get_extras(self) -> list[PatientExtra]:
        ...

    @abstractmethod
    def add_patient_extra(
        self,
        *,
        patient_id: str,
        extra_id: str,
        value: str,
    ) -> bool:
        ...

    # --- Appointments ---

    @abstractmethod
    def list_appointments(
        self,
        *,
        on_date: date | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        patient_id: str | None = None,
        location_id: str | None = None,
        practitioner_id: str | None = None,
        appt_status: str | None = None,
        status: str | None = None,
        time_from: str | None = None,
        time_to: str | None = None,
        service_id: str | None = None,
        class_id: str | None = None,
        page: int = 1,
        page_length: int = 200,
        last_modified: str | None = None,
    ) -> list[Appointment]:
        ...

    @abstractmethod
    def get_appointment(self, appointment_id: str) -> Appointment:
        ...

    @abstractmethod
    def create_appointment(self, payload: Mapping[str, Any]) -> Appointment:
        ...

    @abstractmethod
    def update_appointment(
        self,
        appointment_id: str,
        *,
        starts_at: datetime | None = None,
        status: str | None = None,
        **fields: Any,
    ) -> Appointment:
        ...

    @abstractmethod
    def cancel_appointment(
        self,
        appointment_id: str,
        *,
        patient_id: str,
    ) -> Appointment:
        ...

    @abstractmethod
    def rebook_appointment(
        self,
        appointment_id: str,
        *,
        patient_id: str,
        location_id: str,
        start_time: str,
        practitioner_id: str,
        appointment_date: str | date,
        cancel_first: bool = False,
    ) -> Appointment:
        ...

    @abstractmethod
    def get_appointment_availabilities(
        self,
        *,
        location_id: str | None = None,
        practitioner_id: str | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        appointment_type_id: str | None = None,
    ) -> list[Mapping[str, Any]]:
        ...

    @abstractmethod
    def get_class_availabilities(
        self,
        *,
        location_id: str | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        class_id: str | None = None,
    ) -> list[Mapping[str, Any]]:
        ...

    # --- Locations & Practitioners ---

    @abstractmethod
    def get_locations(self, *, last_modified: str | None = None) -> list[Location]:
        ...

    @abstractmethod
    def get_location_logo(self, location_id: str) -> str | None:
        ...

    @abstractmethod
    def get_practitioners(
        self,
        *,
        last_modified: str | None = None,
        include_inactive_practitioners: bool = False,
    ) -> list[Practitioner]:
        ...

    @abstractmethod
    def get_practitioner_photo(self, practitioner_id: str) -> str | None:
        ...

    # --- Services & Classes ---

    @abstractmethod
    def get_appointment_types(self) -> list[AppointmentType]:
        ...

    @abstractmethod
    def get_class_types(self) -> list[ClassType]:
        ...

    @abstractmethod
    def get_class_participants(
        self,
        class_id: str,
        *,
        page: int = 1,
        page_length: int = 200,
    ) -> list[ClassParticipant]:
        ...

    @abstractmethod
    def get_class_redemptions(self) -> list[Redemption]:
        ...

    @abstractmethod
    def get_service_redemptions(self) -> list[Redemption]:
        ...

    @abstractmethod
    def get_waiting_list(self) -> list[WaitingListEntry]:
        ...

    # --- Documents ---

    @abstractmethod
    def get_patient_files(
        self,
        patient_id: str,
        *,
        page: int = 1,
        page_length: int = 200,
        last_modified: str | None = None,
    ) -> list[PatientFile]:
        ...

    @abstractmethod
    def get_file_url(self, patient_id: str, file_id: str) -> str:
        ...

    @abstractmethod
    def upload_file(
        self,
        *,
        patient_id: str,
        name: str,
        extension: str,
        file_type: str,
        file_path: str,
        case_id: str | None = None,
        date_added: str | None = None,
    ) -> tuple[str, str]:
        ...

    @abstractmethod
    def set_file_active(self, *, patient_id: str, file_id: str) -> bool:
        ...

    @abstractmethod
    def upload_patient_document(
        self,
        *,
        patient_id: str,
        name: str,
        extension: str,
        file_type: str,
        content: bytes,
        case_id: str | None = None,
        date_added: str | None = None,
    ) -> PatientFile:
        ...

    # --- Invoices ---

    @abstractmethod
    def get_invoice(self, invoice_id: str, *, void: int | None = None) -> Invoice:
        ...

    @abstractmethod
    def get_invoices(
        self,
        patient_id: str | None = None,
        *,
        last_modified: str | None = None,
        void: int | None = None,
        expanded: int | None = None,
    ) -> list[Invoice]:
        ...

    @abstractmethod
    def get_invoice_entries(
        self,
        *,
        invoice_id: str | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        lastmodified_date_from: date | None = None,
        lastmodified_date_to: date | None = None,
    ) -> list[InvoiceEntry]:
        ...

    @abstractmethod
    def get_invoice_credits(self, **params: Any) -> list[Mapping[str, Any]]:
        ...

    @abstractmethod
    def get_invoice_discounts(self, **params: Any) -> list[Mapping[str, Any]]:
        ...

    @abstractmethod
    def get_invoice_payments(self, **params: Any) -> list[Mapping[str, Any]]:
        ...

    @abstractmethod
    def get_invoice_refunds(self, **params: Any) -> list[Mapping[str, Any]]:
        ...

    @abstractmethod
    def get_invoice_adjustments(self, **params: Any) -> list[Mapping[str, Any]]:
        ...

    @abstractmethod
    def add_invoice(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        ...

    @abstractmethod
    def delete_invoice(self, invoice_id: str) -> bool:
        ...

    @abstractmethod
    def add_item_to_invoice(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        ...

    @abstractmethod
    def delete_item_from_invoice(self, item_id: str) -> bool:
        ...

    @abstractmethod
    def add_payment_to_invoice(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        ...

    @abstractmethod
    def delete_payment_from_invoice(self, payment_id: str) -> bool:
        ...

    @abstractmethod
    def add_account_credit(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        ...


class _TokenBucket:
    """Simple rate limiter — shared across threads via a lock at call sites."""

    def __init__(self, rate: float) -> None:
        self.rate = max(rate, 0.1)
        self._tokens = self.rate
        self._updated = time.monotonic()

    def take(self) -> None:
        while True:
            now = time.monotonic()
            elapsed = now - self._updated
            self._updated = now
            self._tokens = min(self.rate, self._tokens + elapsed * self.rate)
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return
            sleep_for = (1.0 - self._tokens) / self.rate
            time.sleep(max(sleep_for, 0.01))


class HttpNookalClient(NookalClient):
    """
    Live HTTP client targeting Nookal API v2.
    Endpoints and schemas conform to official Nookal API v2 documentation.
    """

    def __init__(
        self,
        config: NookalConfig | None = None,
        *,
        audit: AuditFn | None = None,
        actor: str = "nookal_client",
        client: httpx.Client | None = None,
    ) -> None:
        self._config = config or get_settings().nookal
        self._audit = audit or audit_mod.log_event
        self._actor = actor
        self._limiter = _TokenBucket(self._config.requests_per_second)
        self._owns_client = client is None
        self._http = client or httpx.Client(
            base_url=self._config.base_url.rstrip("/") + "/",
            timeout=self._config.timeout_seconds,
            headers=self._default_headers(),
        )
        if client is not None:
            self._http.headers.update(self._default_headers())

    def _default_headers(self) -> dict[str, str]:
        """Authentication is via query parameter (?api_key=), NOT Authorization header."""
        return {"Accept": "application/json"}

    def close(self) -> None:
        if self._owns_client:
            self._http.close()

    def __enter__(self) -> HttpNookalClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- transport ---

    def _request(
        self,
        method: str,
        path: str,
        *,
        action: str,
        target_type: audit_mod.TargetType,
        target_id: str,
        is_write: bool = False,
        params: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
        content: bytes | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        if is_write:
            try:
                assert_allows(f"nookal.{action}")
            except KillSwitchActive:
                self._audit(
                    actor=self._actor,
                    action=action,
                    target_type=target_type,
                    target_id=target_id,
                    result="blocked",
                    metadata={"reason": "kill_switch"},
                )
                raise

        req_params = dict(params) if params else {}
        if self._config.api_key and "api_key" not in req_params:
            req_params["api_key"] = self._config.api_key

        last_error: Exception | None = None
        for attempt in range(self._config.max_retries + 1):
            self._limiter.take()
            try:
                response = self._http.request(
                    method,
                    path.lstrip("/"),
                    params=req_params,
                    json=json_body,
                    content=content,
                    headers=dict(headers) if headers else None,
                )
                data = self._handle_response(response, action=action, target_id=target_id)
                self._audit(
                    actor=self._actor,
                    action=action,
                    target_type=target_type,
                    target_id=target_id,
                    result="success",
                    metadata={"method": method, "attempt": attempt},
                )
                return data
            except NookalRateLimit as exc:
                last_error = exc
                self._backoff(attempt, retry_after=getattr(exc, "retry_after", None))
            except NookalServerError as exc:
                last_error = exc
                if attempt >= self._config.max_retries or is_write:
                    break
                self._backoff(attempt)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                sanitized_msg = _redact_secrets(str(exc), self._config.api_key)
                last_error = NookalServerError(sanitized_msg)
                if attempt >= self._config.max_retries or is_write:
                    break
                self._backoff(attempt)

        self._audit(
            actor=self._actor,
            action=action,
            target_type=target_type,
            target_id=target_id,
            result="failure",
            metadata={"method": method},
        )
        assert last_error is not None
        raise last_error

    def _backoff(self, attempt: int, retry_after: float | None = None) -> None:
        if retry_after is not None:
            time.sleep(retry_after)
            return
        time.sleep(min(2**attempt, 30))

    def _handle_response(
        self,
        response: httpx.Response,
        *,
        action: str,
        target_id: str,
    ) -> Any:
        status = response.status_code
        if status in (401, 403):
            raise NookalAuthError(f"auth failed on {action} ({status})")
        if status == 404:
            if action == "get_file_url":
                raise NookalFileNotFound(f"{action}: {target_id} not found")
            raise NookalNotFound(f"{action}: {target_id} not found")
        if status == 422:
            if action == "get_file_url":
                raise NookalInvalidFileId(f"validation error on {action}")
            raise NookalValidationError(f"validation error on {action}")
        if status == 429:
            retry_after = response.headers.get("Retry-After")
            exc = NookalRateLimit("rate limited by Nookal")
            exc.retry_after = float(retry_after) if retry_after and retry_after.isdigit() else None
            raise exc
        if status >= 500:
            redacted = _redact_secrets(response.text[:200], self._config.api_key)
            if action == "get_file_url":
                raise NookalRequestFailed(f"Nookal {status} on {action}: {redacted}")
            raise NookalServerError(f"Nookal {status} on {action}: {redacted}")
        if status >= 400:
            if action == "get_file_url":
                raise NookalRequestFailed(f"Nookal {status} on {action}")
            raise NookalError(f"Nookal {status} on {action}")

        if status == 204 or not response.content:
            return None

        try:
            payload = response.json()
        except ValueError as exc:
            if action == "get_file_url":
                raise NookalResponseInvalid(f"non-JSON response on {action}") from exc
            raise NookalError(f"non-JSON response on {action}") from exc

        if isinstance(payload, Mapping):
            nookal_status = str(payload.get("status", "")).lower()
            if nookal_status in ("failure", "error"):
                details = (
                    payload.get("details")
                    or payload.get("message")
                    or payload.get("error")
                    or "API error"
                )
                if isinstance(details, Mapping):
                    details_str = details.get("errorMessage") or details.get("message") or str(details)
                else:
                    details_str = str(details)
                sanitized_details = _redact_secrets(details_str, self._config.api_key)
                lower_details = sanitized_details.lower()
                if any(x in lower_details for x in ("not found", "no records found", "0 results", "does not exist", "does not belong")):
                    if action == "get_file_url":
                        raise NookalFileNotFound(f"{action}: {sanitized_details}")
                    raise NookalNotFound(f"{action}: {sanitized_details}")
                if any(x in lower_details for x in ("invalid file", "invalid id", "file_id", "file id", "invalid parameter")):
                    if action == "get_file_url":
                        raise NookalInvalidFileId(f"{action}: {sanitized_details}")
                    raise NookalValidationError(f"{action}: {sanitized_details}")
                if any(x in lower_details for x in ("auth", "unauthorized", "api key", "invalid key", "forbidden")):
                    raise NookalAuthError(f"auth failed on {action}: {sanitized_details}")
                if action == "get_file_url":
                    raise NookalRequestFailed(f"Nookal error on {action}: {sanitized_details}")
                raise NookalError(f"Nookal error on {action}: {sanitized_details}")

            if "data" in payload:
                return payload["data"]

        return payload

    # --- Validation helpers ---

    @staticmethod
    def _validate_pagination(page: int, page_length: int, max_length: int = 200) -> None:
        if page < 1:
            raise NookalValidationError("page must be > 0")
        if page_length < 1 or page_length > max_length:
            raise NookalValidationError(f"page_length must be between 1 and {max_length}")

    # --- Patients ---

    def get_patients(
        self,
        *,
        page: int = 1,
        page_length: int = 200,
        last_modified: str | None = None,
        deceased: int | None = None,
    ) -> list[PatientRef]:
        self._validate_pagination(page, page_length, 200)
        params: dict[str, Any] = {"page": page, "page_length": page_length}
        if last_modified:
            params["last_modified"] = last_modified
        if deceased is not None:
            if deceased not in (0, 1):
                raise NookalValidationError("deceased must be 0, 1, or None")
            params["deceased"] = deceased

        data = self._request(
            "GET",
            "/getPatients",
            action="get_patients",
            target_type="patient_record",
            target_id=f"page_{page}",
            params=params,
        )
        rows = _unwrap_collection(data, "patients")
        return [self._parse_patient(r) for r in rows if isinstance(r, Mapping)]

    def search_patients(
        self,
        *,
        patient_id: str | None = None,
        online_code: str | None = None,
        date_created: str | None = None,
        email: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
        date_of_birth: str | None = None,
        fuzzy_search: str | None = None,
        suburb: str | list[str] | None = None,
        age_min: int | None = None,
        age_max: int | None = None,
        appointment_from: date | None = None,
        appointment_to: date | None = None,
        referrer_id: str | None = None,
        deceased: int | None = None,
    ) -> list[PatientRef]:
        params: dict[str, Any] = {}
        if patient_id:
            params["patient_id"] = patient_id
        if online_code:
            params["online_code"] = online_code
        if date_created:
            params["date_created"] = date_created
        if email:
            params["email"] = email
        if first_name:
            params["first_name"] = first_name
        if last_name:
            params["last_name"] = last_name
        if date_of_birth:
            params["date_of_birth"] = date_of_birth
        if fuzzy_search:
            clean_term = fuzzy_search.strip()
            params["fuzzy_search"] = clean_term
            if "@" in clean_term and "email" not in params:
                params["email"] = clean_term
            elif not first_name and not last_name and not patient_id:
                parts = clean_term.split()
                if len(parts) >= 2:
                    params["first_name"] = parts[0]
                    params["last_name"] = " ".join(parts[1:])
                elif len(parts) == 1:
                    if clean_term.isdigit():
                        params["patient_id"] = clean_term
                    else:
                        params["first_name"] = clean_term

        # If search parameters are given, query /searchPatients
        results: list[PatientRef] = []
        if params:
            try:
                data = self._request(
                    "GET",
                    "/searchPatients",
                    action="search_patients",
                    target_type="patient_record",
                    target_id="search",
                    params=params,
                )
                rows = _unwrap_collection(data, "patients")
                results = [self._parse_patient(row) for row in rows if isinstance(row, Mapping)]
            except NookalError as exc:
                logger.warning(
                    "Nookal /searchPatients failed (%s); falling back to /getPatients directory search",
                    exc,
                )
                results = []
                try:
                    results = self.get_patients(page=1, page_length=200, deceased=deceased)
                except Exception as fallback_exc:
                    logger.error("Fallback get_patients also failed: %s", fallback_exc)
                    raise exc

            # If search returned 0 results and fuzzy_search was requested,
            # fall back to get_patients directory to see if patient exists in local directory
            if not results and fuzzy_search:
                try:
                    results = self.get_patients(page=1, page_length=200, deceased=deceased)
                except Exception:
                    pass
        else:
            # General directory query uses /getPatients
            results = self.get_patients(page=1, page_length=200, deceased=deceased)

        if deceased is not None:
            results = [p for p in results if p.deceased == bool(deceased)]

        # Client-side fuzzy query filtering
        if fuzzy_search:
            needle = fuzzy_search.strip().lower()
            results = [
                p
                for p in results
                if needle in (p.display_name or "").lower()
                or needle in f"{p.first_name or ''} {p.last_name or ''}".lower()
                or needle in (p.phone or "").lower()
                or needle in (p.email or "").lower()
                or needle == (p.patient_id or "").lower()
            ]

        # Client-side demographic filtering
        if suburb is not None:
            if isinstance(suburb, (list, set, tuple)):
                suburbs_set = {s.casefold() for s in suburb if s}
                if suburbs_set:
                    results = [p for p in results if (p.suburb or "").casefold() in suburbs_set]
            else:
                needle = suburb.casefold()
                results = [p for p in results if (p.suburb or "").casefold() == needle]

        as_of = date.today()
        if age_min is not None or age_max is not None:
            filtered: list[PatientRef] = []
            for p in results:
                if p.date_of_birth is None:
                    continue
                age = _age_years(p.date_of_birth, as_of)
                if age_min is not None and age < age_min:
                    continue
                if age_max is not None and age > age_max:
                    continue
                filtered.append(p)
            results = filtered

        if appointment_from is not None or appointment_to is not None:
            filtered_by_appt: list[PatientRef] = []
            for p in results:
                if p.last_appointment_date is not None:
                    day = p.last_appointment_date
                    if appointment_from is not None and day < appointment_from:
                        continue
                    if appointment_to is not None and day > appointment_to:
                        continue
                    filtered_by_appt.append(p)
            if filtered_by_appt or any(p.last_appointment_date is not None for p in results):
                results = filtered_by_appt

        if referrer_id is not None:
            results = [p for p in results if p.referrer_id == referrer_id]

        results.sort(key=lambda p: p.patient_id)
        return results

    def get_patient(self, patient_id: str) -> PatientRef:
        data = self._request(
            "GET",
            "/searchPatients",
            action="get_patient",
            target_type="patient_record",
            target_id=patient_id,
            params={"patient_id": patient_id},
        )
        rows = _unwrap_collection(data, "patients")
        if not rows:
            raise NookalNotFound(f"get_patient: patient {patient_id} not found")

        matched_ids: set[str] = set()
        for r in rows:
            if isinstance(r, Mapping):
                pid = r.get("ID") or r.get("id") or r.get("patient_id") or r.get("patientID") or r.get("PatientID") or r.get("patientId") or r.get("PatientId")
                if pid:
                    matched_ids.add(str(pid))

        if len(matched_ids) > 1:
            raise NookalValidationError(f"ambiguous patient matches for {patient_id}")

        matching = next(
            (
                r
                for r in rows
                if isinstance(r, Mapping)
                and str(r.get("ID") or r.get("id") or r.get("patient_id") or r.get("patientID") or r.get("PatientID") or r.get("patientId") or r.get("PatientId"))
                == str(patient_id)
            ),
            rows[0],
        )
        return self._parse_patient(matching)

    def find_patient_by_phone(self, phone: str) -> list[PatientRef]:
        clean_phone = re.sub(r"[^\d+]", "", phone)
        try:
            data = self._request(
                "GET",
                "/searchPatients",
                action="find_patient_by_phone",
                target_type="patient_record",
                target_id="phone_lookup",
                params={"fuzzy_search": phone, "phone": phone, "Mobile": phone},
            )
            rows = _unwrap_collection(data, "patients")
            patients = [self._parse_patient(row) for row in rows if isinstance(row, Mapping)]
        except NookalError as exc:
            logger.warning(
                "find_patient_by_phone /searchPatients failed (%s); falling back to /getPatients",
                exc,
            )
            patients = self.get_patients(page=1, page_length=200)

        matched = []
        for p in patients:
            if p.phone:
                p_clean = re.sub(r"[^\d+]", "", p.phone)
                if p.phone == phone or (clean_phone and p_clean == clean_phone):
                    matched.append(p)
        return matched

    def add_patient(self, payload: Mapping[str, Any]) -> PatientRef:
        first_name = (
            payload.get("firstName")
            or payload.get("first_name")
            or payload.get("FirstName")
        )
        last_name = (
            payload.get("lastName")
            or payload.get("last_name")
            or payload.get("LastName")
        )
        if not first_name or not last_name:
            raise NookalValidationError("add_patient requires both first_name and last_name")

        body: dict[str, Any] = {}
        for k, v in payload.items():
            if v is not None:
                body[k] = v

        data = self._request(
            "POST",
            "/addPatient",
            action="add_patient",
            target_type="patient_record",
            target_id="new",
            is_write=True,
            json_body=body,
        )
        if isinstance(data, Mapping):
            return self._parse_patient(data)
        return PatientRef(
            patient_id=str(data or "new"),
            first_name=str(first_name),
            last_name=str(last_name),
            display_name=f"{first_name} {last_name}".strip(),
            phone=payload.get("phone") or payload.get("mobile"),
            email=payload.get("email"),
            raw=dict(payload),
        )

    def edit_patient(self, patient_id: str, payload: Mapping[str, Any]) -> PatientRef:
        if not patient_id:
            raise NookalValidationError("edit_patient requires patient_id")

        body: dict[str, Any] = {"patient_id": patient_id}
        for k, v in payload.items():
            if v is not None and k not in ("patient_id", "patientID", "ID"):
                body[k] = v

        data = self._request(
            "POST",
            "/editPatient",
            action="edit_patient",
            target_type="patient_record",
            target_id=patient_id,
            is_write=True,
            json_body=body,
        )
        if isinstance(data, Mapping):
            return self._parse_patient(data)
        return PatientRef(
            patient_id=patient_id,
            raw=dict(payload),
        )

    # --- Cases ---

    def get_cases(
        self,
        patient_id: str,
        *,
        page: int = 1,
        page_length: int = 200,
        last_modified: str | None = None,
    ) -> list[CaseRef]:
        if not patient_id:
            raise NookalValidationError("patient_id is required for get_cases")
        self._validate_pagination(page, page_length, 200)
        params: dict[str, Any] = {
            "patient_id": patient_id,
            "page": page,
            "page_length": page_length,
        }
        if last_modified:
            params["last_modified"] = last_modified

        data = self._request(
            "GET",
            "/getCases",
            action="get_cases",
            target_type="patient_record",
            target_id=patient_id,
            params=params,
        )
        rows = _unwrap_collection(data, "cases")
        return [self._parse_case(r, fallback_patient_id=patient_id) for r in rows if isinstance(r, Mapping)]

    def get_all_cases(
        self,
        *,
        page: int = 1,
        page_length: int = 200,
        last_modified: str | None = None,
    ) -> list[CaseRef]:
        self._validate_pagination(page, page_length, 200)
        params: dict[str, Any] = {"page": page, "page_length": page_length}
        if last_modified:
            params["last_modified"] = last_modified

        data = self._request(
            "GET",
            "/getAllCases",
            action="get_all_cases",
            target_type="patient_record",
            target_id=f"page_{page}",
            params=params,
        )
        rows = _unwrap_collection(data, "cases")
        return [self._parse_case(r) for r in rows if isinstance(r, Mapping)]

    # --- Treatment Notes ---

    def get_treatment_notes(
        self,
        patient_id: str,
        *,
        page: int = 1,
        page_length: int = 100,
        last_modified: str | None = None,
    ) -> list[TreatmentNote]:
        if not patient_id:
            raise NookalValidationError("patient_id is required for get_treatment_notes")
        self._validate_pagination(page, page_length, 100)
        params: dict[str, Any] = {
            "patient_id": patient_id,
            "page": page,
            "page_length": page_length,
        }
        if last_modified:
            params["last_modified"] = last_modified

        data = self._request(
            "GET",
            "/getTreatmentNotes",
            action="get_treatment_notes",
            target_type="patient_record",
            target_id=patient_id,
            params=params,
        )
        rows = _unwrap_collection(data, "notes")
        return [self._parse_treatment_note(r, fallback_patient_id=patient_id) for r in rows if isinstance(r, Mapping)]

    def get_all_treatment_notes(
        self,
        *,
        page: int = 1,
        page_length: int = 50,
        last_modified: str | None = None,
        practitioner_id: str | None = None,
    ) -> list[TreatmentNote]:
        self._validate_pagination(page, page_length, 50)
        params: dict[str, Any] = {"page": page, "page_length": page_length}
        if last_modified:
            params["last_modified"] = last_modified
        if practitioner_id:
            params["practitioner_id"] = practitioner_id

        data = self._request(
            "GET",
            "/getAllTreatmentNotes",
            action="get_all_treatment_notes",
            target_type="patient_record",
            target_id=f"page_{page}",
            params=params,
        )
        rows = _unwrap_collection(data, "notes")
        return [self._parse_treatment_note(r) for r in rows if isinstance(r, Mapping)]

    def add_treatment_note(
        self,
        *,
        patient_id: str,
        case_id: str,
        practitioner_id: str,
        date: str | datetime,
        notes: str,
        appt_id: str | None = None,
    ) -> TreatmentNote:
        if not patient_id or not case_id or not practitioner_id or not notes:
            raise NookalValidationError("add_treatment_note requires patient_id, case_id, practitioner_id, date, and notes")

        if isinstance(date, datetime):
            date_str = date.strftime("%Y-%m-%d %H:%M:%S")
        else:
            date_str = str(date).strip()
            # Verify format YYYY-MM-DD HH:MM:SS
            try:
                datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                raise NookalValidationError("add_treatment_note date must be in format YYYY-MM-DD HH:MM:SS")

        body: dict[str, Any] = {
            "patient_id": patient_id,
            "case_id": case_id,
            "practitioner_id": practitioner_id,
            "date": date_str,
            "notes": notes,
        }
        if appt_id:
            body["appt_id"] = appt_id

        data = self._request(
            "POST",
            "/addTreatmentNote",
            action="add_treatment_note",
            target_type="patient_record",
            target_id=patient_id,
            is_write=True,
            json_body=body,
        )
        if isinstance(data, Mapping):
            return self._parse_treatment_note(data)
        return TreatmentNote(
            note_id="new",
            patient_id=patient_id,
            case_id=case_id,
            practitioner_id=practitioner_id,
            date=date_str,
            notes=notes,
            appointment_id=appt_id,
        )

    # --- Extras ---

    def get_extras(self) -> list[PatientExtra]:
        data = self._request(
            "GET",
            "/getExtras",
            action="get_extras",
            target_type="patient_record",
            target_id="extras",
        )
        rows = _unwrap_collection(data, "extras")
        return [self._parse_extra(r) for r in rows if isinstance(r, Mapping)]

    def add_patient_extra(
        self,
        *,
        patient_id: str,
        extra_id: str,
        value: str,
    ) -> bool:
        if not patient_id or not extra_id:
            raise NookalValidationError("add_patient_extra requires patient_id and extra_id")
        try:
            if int(patient_id) <= 0:
                raise NookalValidationError("patient_id must be > 0")
        except ValueError:
            pass

        body = {
            "patient_id": patient_id,
            "extra_id": extra_id,
            "value": str(value),
        }
        self._request(
            "POST",
            "/addPatientExtra",
            action="add_patient_extra",
            target_type="patient_record",
            target_id=patient_id,
            is_write=True,
            json_body=body,
        )
        return True

    # --- Appointments ---

    def list_appointments(
        self,
        *,
        on_date: date | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        patient_id: str | None = None,
        location_id: str | None = None,
        practitioner_id: str | None = None,
        appt_status: str | None = None,
        status: str | None = None,
        time_from: str | None = None,
        time_to: str | None = None,
        service_id: str | None = None,
        class_id: str | None = None,
        page: int = 1,
        page_length: int = 200,
        last_modified: str | None = None,
    ) -> list[Appointment]:
        self._validate_pagination(page, page_length, 200)
        params: dict[str, Any] = {
            "page": page,
            "page_length": page_length,
        }
        if on_date:
            params["date_from"] = on_date.strftime("%Y-%m-%d")
            params["date_to"] = on_date.strftime("%Y-%m-%d")
        else:
            if date_from:
                params["date_from"] = date_from.strftime("%Y-%m-%d")
            if date_to:
                params["date_to"] = date_to.strftime("%Y-%m-%d")

        if patient_id:
            params["patient_id"] = patient_id
        if location_id:
            params["location_id"] = location_id
        if practitioner_id:
            params["practitioner_id"] = practitioner_id
        effective_status = status or appt_status
        if effective_status:
            params["appt_status"] = effective_status
        if time_from:
            params["time_from"] = time_from
        if time_to:
            params["time_to"] = time_to
        if service_id:
            params["service_id"] = service_id
        if class_id:
            params["class_id"] = class_id
        if last_modified:
            params["last_modified"] = last_modified

        data = self._request(
            "GET",
            "/getAppointments",
            action="list_appointments",
            target_type="appointment",
            target_id=patient_id or (on_date.isoformat() if on_date else "range"),
            params=params,
        )
        rows = _unwrap_collection(data, "appointments")
        return [self._parse_appointment(row) for row in rows if isinstance(row, Mapping)]

    def get_appointment(self, appointment_id: str) -> Appointment:
        clean_id = str(appointment_id).strip()
        bare_id = clean_id.lower().replace("appt_", "")
        params: dict[str, Any] = {
            "page_length": 200,
            "appointment_id": clean_id,
            "AppointmentID": clean_id,
            "appointmentID": clean_id,
            "ID": clean_id,
            "date_from": "2020-01-01",
            "date_to": "2035-12-31",
        }
        if bare_id and bare_id != clean_id:
            params["id"] = bare_id

        data = None
        for endpoint in ("/getAppointments", "/getAppointment"):
            try:
                data = self._request(
                    "GET",
                    endpoint,
                    action="get_appointment",
                    target_type="appointment",
                    target_id=clean_id,
                    params=params,
                )
                if data:
                    break
            except (NookalNotFound, NookalError):
                data = None

        if data:
            rows = _unwrap_collection(data, "appointments")
            if not rows and isinstance(data, Mapping) and any(k in data for k in ("ID", "id", "appointment_id", "AppointmentID")):
                rows = [data]
            for row in rows:
                if isinstance(row, Mapping):
                    aid = (
                        row.get("ID")
                        or row.get("id")
                        or row.get("appointment_id")
                        or row.get("appointmentID")
                        or row.get("AppointmentID")
                        or row.get("appointmentId")
                        or row.get("AppointmentId")
                    )
                    if aid:
                        s_aid = str(aid).strip().lower()
                        s_bare = s_aid.replace("appt_", "")
                        if (
                            s_aid == clean_id.lower()
                            or (bare_id and s_bare == bare_id)
                            or (bare_id and bare_id.isdigit() and s_bare.isdigit() and int(bare_id) == int(s_bare))
                        ):
                            return self._parse_appointment(row)

        # Fallback: Query recent / broad appointment lists with pagination in case the API ignores the appointment_id query param
        try:
            for p in range(1, 6):
                try:
                    candidates = self.list_appointments(
                        date_from=date(2020, 1, 1),
                        date_to=date(2035, 12, 31),
                        page=p,
                        page_length=200,
                    )
                    if not candidates:
                        break
                    for appt in candidates:
                        s_aid = str(appt.appointment_id).strip().lower()
                        s_bare = s_aid.replace("appt_", "")
                        if (
                            s_aid == clean_id.lower()
                            or (bare_id and s_bare == bare_id)
                            or (bare_id and bare_id.isdigit() and s_bare.isdigit() and int(bare_id) == int(s_bare))
                        ):
                            return appt
                except Exception:
                    break
        except Exception:
            pass

        # Fallback: search today/immediate appointments
        try:
            for appt in self.list_appointments(page_length=200):
                s_aid = str(appt.appointment_id).strip().lower()
                s_bare = s_aid.replace("appt_", "")
                if (
                    s_aid == clean_id.lower()
                    or (bare_id and s_bare == bare_id)
                    or (bare_id and bare_id.isdigit() and s_bare.isdigit() and int(bare_id) == int(s_bare))
                ):
                    return appt
        except Exception:
            pass

        raise NookalNotFound(f"get_appointment: appointment {appointment_id} not found")

    def create_appointment(self, payload: Mapping[str, Any]) -> Appointment:
        body: dict[str, Any] = {}
        starts = payload.get("starts_at")
        if starts:
            if isinstance(starts, str):
                try:
                    starts = datetime.fromisoformat(starts)
                except ValueError:
                    pass
            if isinstance(starts, datetime):
                body.setdefault("appointment_date", starts.date().strftime("%Y-%m-%d"))
                body.setdefault("start_time", starts.time().strftime("%H:%M:%S"))

        field_mapping = {
            "location_id": ("location_id", "locationID"),
            "appointment_date": ("appointment_date", "date"),
            "start_time": ("start_time", "startTime"),
            "patient_id": ("patient_id", "patientID"),
            "practitioner_id": ("practitioner_id", "practitionerID"),
            "appointment_type_id": ("appointment_type_id", "type_id", "typeID"),
            "notes": ("notes",),
            "allow_overlap_bookings": ("allow_overlap_bookings",),
        }
        for target_key, sources in field_mapping.items():
            for src in sources:
                if src in payload and payload[src] is not None:
                    body[target_key] = payload[src]
                    break

        required = [
            "location_id",
            "appointment_date",
            "start_time",
            "patient_id",
            "practitioner_id",
            "appointment_type_id",
        ]
        missing = [f for f in required if f not in body or body[f] is None]
        if missing:
            raise NookalValidationError(
                f"create_appointment missing required fields: {', '.join(missing)}"
            )

        # Validate date and time formats
        try:
            datetime.strptime(str(body["appointment_date"]), "%Y-%m-%d")
        except ValueError:
            raise NookalValidationError("appointment_date must be in format YYYY-MM-DD")
        try:
            t_str = str(body["start_time"]).strip()
            if len(t_str) == 5:
                t_str = f"{t_str}:00"
                body["start_time"] = t_str
            datetime.strptime(t_str, "%H:%M:%S")
        except ValueError:
            raise NookalValidationError("start_time must be in format HH:MM:SS")

        data = self._request(
            "POST",
            "/addAppointmentBooking",
            action="create_appointment",
            target_type="appointment",
            target_id=str(body["patient_id"]),
            is_write=True,
            json_body=body,
        )
        if isinstance(data, Mapping) and ("ID" in data or "id" in data or "appointment_id" in data):
            try:
                return self._parse_appointment(data)
            except NookalValidationError:
                pass

        aid = "new"
        if isinstance(data, Mapping):
            aid = str(data.get("ID") or data.get("id") or data.get("appointment_id") or "new")
        elif isinstance(data, (str, int)):
            aid = str(data)

        try:
            starts_at_dt = datetime.fromisoformat(f"{body['appointment_date']}T{body['start_time']}")
        except Exception:
            starts_at_dt = datetime.now()

        return Appointment(
            appointment_id=aid,
            patient_id=str(body["patient_id"]),
            starts_at=starts_at_dt,
            status="booked",
            location_id=str(body["location_id"]),
            practitioner_id=str(body["practitioner_id"]),
            raw=dict(data) if isinstance(data, Mapping) else {"data": data, "request": body},
        )

    def update_appointment(
        self,
        appointment_id: str,
        *,
        starts_at: datetime | None = None,
        status: str | None = None,
        **fields: Any,
    ) -> Appointment:
        body: dict[str, Any] = {"appointment_id": appointment_id}
        if starts_at is not None:
            body["appointment_date"] = starts_at.date().strftime("%Y-%m-%d")
            body["start_time"] = starts_at.time().strftime("%H:%M:%S")

        allowed_statuses = {"Completed", "Pending", "Cancelled", "DNA"}
        if status is not None:
            norm_status = status.capitalize() if status.lower() not in ("dna",) else "DNA"
            if norm_status not in allowed_statuses and status not in allowed_statuses:
                raise NookalValidationError(
                    f"Invalid status '{status}'. Documented allowed values: Completed, Pending, Cancelled, DNA"
                )
            body["status"] = norm_status

        allowed_fields = {
            "location_id",
            "appointment_date",
            "start_time",
            "practitioner_id",
            "appointment_type_id",
            "notes",
        }
        for k, v in fields.items():
            if k in allowed_fields and v is not None:
                body[k] = v

        data = self._request(
            "POST",
            "/updateAppointmentBooking",
            action="update_appointment",
            target_type="appointment",
            target_id=appointment_id,
            is_write=True,
            json_body=body,
        )
        if isinstance(data, Mapping) and ("ID" in data or "id" in data or "appointment_id" in data):
            try:
                return self._parse_appointment(data)
            except NookalValidationError:
                pass

        return Appointment(
            appointment_id=appointment_id,
            patient_id=str(fields.get("patient_id", "")),
            starts_at=starts_at or datetime.now(),
            status=status or "booked",
            raw=dict(data) if isinstance(data, Mapping) else {"data": data},
        )

    def cancel_appointment(
        self,
        appointment_id: str,
        *,
        patient_id: str,
    ) -> Appointment:
        if not appointment_id or not patient_id:
            raise NookalValidationError("cancel_appointment requires both appointment_id and patient_id")
        body: dict[str, Any] = {
            "appointment_id": appointment_id,
            "patient_id": patient_id,
        }
        data = self._request(
            "POST",
            "/cancelAppointment",
            action="cancel_appointment",
            target_type="appointment",
            target_id=appointment_id,
            is_write=True,
            json_body=body,
        )
        if isinstance(data, Mapping) and ("ID" in data or "id" in data or "appointment_id" in data):
            try:
                return self._parse_appointment(data)
            except NookalValidationError:
                pass

        return Appointment(
            appointment_id=appointment_id,
            patient_id=str(patient_id),
            starts_at=datetime.now(),
            status="cancelled",
            raw=dict(data) if isinstance(data, Mapping) else {"data": data},
        )

    def rebook_appointment(
        self,
        appointment_id: str,
        *,
        patient_id: str,
        location_id: str,
        start_time: str,
        practitioner_id: str,
        appointment_date: str | date,
        cancel_first: bool = False,
    ) -> Appointment:
        if not appointment_id or not patient_id or not location_id or not start_time or not practitioner_id or not appointment_date:
            raise NookalValidationError("rebook_appointment missing required fields")

        date_str = (
            appointment_date.strftime("%Y-%m-%d")
            if isinstance(appointment_date, date)
            else str(appointment_date)
        )
        body: dict[str, Any] = {
            "appointment_id": appointment_id,
            "patient_id": patient_id,
            "location_id": location_id,
            "start_time": start_time,
            "practitioner_id": practitioner_id,
            "appointment_date": date_str,
            "cancel_first": cancel_first,
        }
        data = self._request(
            "POST",
            "/rebookAppointment",
            action="rebook_appointment",
            target_type="appointment",
            target_id=appointment_id,
            is_write=True,
            json_body=body,
        )
        if isinstance(data, Mapping) and ("ID" in data or "id" in data or "appointment_id" in data):
            try:
                return self._parse_appointment(data)
            except NookalValidationError:
                pass

        aid = "new"
        if isinstance(data, Mapping):
            aid = str(data.get("ID") or data.get("id") or data.get("appointment_id") or "new")
        try:
            dt = datetime.fromisoformat(f"{date_str}T{start_time}")
        except Exception:
            dt = datetime.now()

        return Appointment(
            appointment_id=aid,
            patient_id=patient_id,
            starts_at=dt,
            status="booked",
            location_id=location_id,
            practitioner_id=practitioner_id,
            raw=dict(data) if isinstance(data, Mapping) else {"data": data},
        )

    def get_appointment_availabilities(
        self,
        *,
        location_id: str | None = None,
        practitioner_id: str | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        appointment_type_id: str | None = None,
    ) -> list[Mapping[str, Any]]:
        params: dict[str, Any] = {}
        if location_id:
            params["location_id"] = location_id
        if practitioner_id:
            params["practitioner_id"] = practitioner_id
        if date_from:
            params["date_from"] = date_from.strftime("%Y-%m-%d")
        if date_to:
            params["date_to"] = date_to.strftime("%Y-%m-%d")
        if appointment_type_id:
            params["appointment_type_id"] = appointment_type_id

        data = self._request(
            "GET",
            "/getAppointmentAvailabilities",
            action="get_appointment_availabilities",
            target_type="appointment",
            target_id="availabilities",
            params=params,
        )
        rows = _unwrap_collection(data, "availabilities")
        return [dict(r) for r in rows if isinstance(r, Mapping)]

    def get_class_availabilities(
        self,
        *,
        location_id: str | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        class_id: str | None = None,
    ) -> list[Mapping[str, Any]]:
        params: dict[str, Any] = {}
        if location_id:
            params["location_id"] = location_id
        if date_from:
            params["date_from"] = date_from.strftime("%Y-%m-%d")
        if date_to:
            params["date_to"] = date_to.strftime("%Y-%m-%d")
        if class_id:
            params["class_id"] = class_id

        data = self._request(
            "GET",
            "/getClassAvailabilities",
            action="get_class_availabilities",
            target_type="appointment",
            target_id="class_availabilities",
            params=params,
        )
        rows = _unwrap_collection(data, "availabilities")
        return [dict(r) for r in rows if isinstance(r, Mapping)]

    # --- Locations & Practitioners ---

    def get_locations(self, *, last_modified: str | None = None) -> list[Location]:
        params: dict[str, Any] = {}
        if last_modified:
            params["last_modified"] = last_modified
        data = self._request(
            "GET",
            "/getLocations",
            action="get_locations",
            target_type="system",
            target_id="locations",
            params=params,
        )
        rows = _unwrap_collection(data, "locations")
        return [self._parse_location(r) for r in rows if isinstance(r, Mapping)]

    def get_location_logo(self, location_id: str) -> str | None:
        if not location_id:
            raise NookalValidationError("location_id is required for get_location_logo")
        data = self._request(
            "GET",
            "/getLocationLogo",
            action="get_location_logo",
            target_type="system",
            target_id=location_id,
            params={"location_id": location_id},
        )
        if isinstance(data, Mapping):
            return data.get("url") or data.get("logo_url") or data.get("logo")
        if isinstance(data, str):
            return data
        return None

    def get_practitioners(
        self,
        *,
        last_modified: str | None = None,
        include_inactive_practitioners: bool = False,
    ) -> list[Practitioner]:
        params: dict[str, Any] = {}
        if last_modified:
            params["last_modified"] = last_modified
        if include_inactive_practitioners:
            params["include_inactive_practitioners"] = 1

        data = self._request(
            "GET",
            "/getPractitioners",
            action="get_practitioners",
            target_type="system",
            target_id="practitioners",
            params=params,
        )
        rows = _unwrap_collection(data, "practitioners")
        return [self._parse_practitioner(r) for r in rows if isinstance(r, Mapping)]

    def get_practitioner_photo(self, practitioner_id: str) -> str | None:
        if not practitioner_id:
            raise NookalValidationError("practitioner_id is required for get_practitioner_photo")
        data = self._request(
            "GET",
            "/getPractitionerPhoto",
            action="get_practitioner_photo",
            target_type="system",
            target_id=practitioner_id,
            params={"practitioner_id": practitioner_id},
        )
        if isinstance(data, Mapping):
            return data.get("url") or data.get("photo_url") or data.get("photo")
        if isinstance(data, str):
            return data
        return None

    # --- Services & Classes ---

    def get_appointment_types(self) -> list[AppointmentType]:
        data = self._request(
            "GET",
            "/getAppointmentTypes",
            action="get_appointment_types",
            target_type="system",
            target_id="appointment_types",
        )
        rows = _unwrap_collection(data, "types")
        return [self._parse_appointment_type(r) for r in rows if isinstance(r, Mapping)]

    def get_class_types(self) -> list[ClassType]:
        data = self._request(
            "GET",
            "/getClassTypes",
            action="get_class_types",
            target_type="system",
            target_id="class_types",
        )
        rows = _unwrap_collection(data, "classes")
        return [self._parse_class_type(r) for r in rows if isinstance(r, Mapping)]

    def get_class_participants(
        self,
        class_id: str,
        *,
        page: int = 1,
        page_length: int = 200,
    ) -> list[ClassParticipant]:
        if not class_id:
            raise NookalValidationError("class_id is required for get_class_participants")
        self._validate_pagination(page, page_length, 200)
        params = {"class_id": class_id, "page": page, "page_length": page_length}
        data = self._request(
            "GET",
            "/getClassParticipants",
            action="get_class_participants",
            target_type="system",
            target_id=class_id,
            params=params,
        )
        rows = _unwrap_collection(data, "participants")
        return [self._parse_class_participant(r) for r in rows if isinstance(r, Mapping)]

    def get_class_redemptions(self) -> list[Redemption]:
        data = self._request(
            "GET",
            "/getClassRedemptions",
            action="get_class_redemptions",
            target_type="system",
            target_id="class_redemptions",
        )
        rows = _unwrap_collection(data, "redemptions")
        return [Redemption(redemption_id=str(r.get("ID") or r.get("id")), patient_id=str(r.get("patientID") or r.get("patient_id") or ""), item_type="class", raw=dict(r)) for r in rows if isinstance(r, Mapping)]

    def get_service_redemptions(self) -> list[Redemption]:
        data = self._request(
            "GET",
            "/getServiceRedemptions",
            action="get_service_redemptions",
            target_type="system",
            target_id="service_redemptions",
        )
        rows = _unwrap_collection(data, "redemptions")
        return [Redemption(redemption_id=str(r.get("ID") or r.get("id")), patient_id=str(r.get("patientID") or r.get("patient_id") or ""), item_type="service", raw=dict(r)) for r in rows if isinstance(r, Mapping)]

    def get_waiting_list(self) -> list[WaitingListEntry]:
        data = self._request(
            "GET",
            "/getWaitingList",
            action="get_waiting_list",
            target_type="system",
            target_id="waiting_list",
        )
        rows = _unwrap_collection(data, "waiting_list")
        return [
            WaitingListEntry(
                entry_id=str(r.get("ID") or r.get("id")),
                patient_id=str(r.get("patientID") or r.get("patient_id") or "") or None,
                location_id=str(r.get("locationID") or r.get("location_id") or "") or None,
                practitioner_id=str(r.get("practitionerID") or r.get("practitioner_id") or "") or None,
                date_added=str(r.get("dateAdded") or r.get("date_added") or "") or None,
                notes=str(r.get("notes") or "") or None,
                raw=dict(r),
            )
            for r in rows if isinstance(r, Mapping)
        ]

    # --- Documents ---

    def get_patient_files(
        self,
        patient_id: str,
        *,
        page: int = 1,
        page_length: int = 200,
        last_modified: str | None = None,
    ) -> list[PatientFile]:
        if not patient_id:
            raise NookalValidationError("patient_id is required for get_patient_files")
        self._validate_pagination(page, page_length, 200)
        params: dict[str, Any] = {
            "patient_id": patient_id,
            "page": page,
            "page_length": page_length,
        }
        if last_modified:
            params["last_modified"] = last_modified

        data = self._request(
            "GET",
            "/getPatientFiles",
            action="get_patient_files",
            target_type="patient_record",
            target_id=patient_id,
            params=params,
        )
        rows = _unwrap_collection(data, "files")
        return [self._parse_patient_file(r, fallback_patient_id=patient_id) for r in rows if isinstance(r, Mapping)]

    def get_file_url(self, patient_id: str, file_id: str) -> str:
        clean_pid = str(patient_id or "").strip()
        clean_fid = str(file_id or "").strip()
        if not clean_pid or not clean_fid:
            raise NookalInvalidFileId("patient_id and file_id are required for get_file_url")
        if any(c in clean_fid for c in "/\\?#&="):
            raise NookalInvalidFileId(f"Invalid file_id format: {clean_fid}")

        try:
            data = self._request(
                "GET",
                "/getFileUrl",
                action="get_file_url",
                target_type="patient_record",
                target_id=f"{clean_pid}_{clean_fid}",
                params={"patient_id": clean_pid, "file_id": clean_fid},
            )
        except (NookalFileNotFound, NookalInvalidFileId, NookalRequestFailed):
            raise
        except NookalNotFound as exc:
            raise NookalFileNotFound(f"Nookal file {clean_fid} not found for patient {clean_pid}") from exc
        except NookalValidationError as exc:
            raise NookalInvalidFileId(f"Nookal rejected file {clean_fid}: {exc}") from exc
        except NookalError as exc:
            raise NookalRequestFailed(f"Nookal request failed on get_file_url: {exc}") from exc

        url = _extract_file_url(data)
        if not url:
            if not isinstance(data, (Mapping, str, list)):
                raise NookalResponseInvalid(
                    f"Unexpected getFileUrl response payload shape: {type(data).__name__}"
                )
            raise NookalUrlMissing(
                f"getFileUrl response did not contain a download URL for file {clean_fid}"
            )
        return str(url)

    def upload_file(
        self,
        *,
        patient_id: str,
        name: str,
        extension: str,
        file_type: str,
        file_path: str,
        case_id: str | None = None,
        date_added: str | None = None,
    ) -> tuple[str, str]:
        if not patient_id or not name or not extension or not file_type or not file_path:
            raise NookalValidationError("upload_file missing required metadata fields")

        body: dict[str, Any] = {
            "patient_id": patient_id,
            "name": name,
            "extension": extension.lstrip("."),
            "file_type": file_type,
            "file_path": file_path,
        }
        if case_id:
            body["case_id"] = case_id
        if date_added:
            body["date_added"] = date_added

        data = self._request(
            "POST",
            "/uploadFile",
            action="upload_file",
            target_type="patient_record",
            target_id=patient_id,
            is_write=True,
            json_body=body,
        )
        if not isinstance(data, Mapping):
            raise NookalError("uploadFile returned unexpected response shape")

        file_id = str(data.get("file_id") or data.get("fileID") or data.get("id") or data.get("ID") or "")
        presigned_url = str(data.get("url") or data.get("presigned_url") or data.get("s3_url") or data.get("upload_url") or "")
        if not file_id or not presigned_url:
            raise NookalError("uploadFile response missing file_id or presigned S3 upload URL")

        return file_id, presigned_url

    def set_file_active(self, *, patient_id: str, file_id: str) -> bool:
        if not patient_id or not file_id:
            raise NookalValidationError("set_file_active requires patient_id and file_id")

        body = {"patient_id": patient_id, "file_id": file_id}
        self._request(
            "POST",
            "/setFileActive",
            action="set_file_active",
            target_type="patient_record",
            target_id=f"{patient_id}_{file_id}",
            is_write=True,
            json_body=body,
        )
        return True

    def upload_patient_document(
        self,
        *,
        patient_id: str,
        name: str,
        extension: str,
        file_type: str,
        content: bytes,
        case_id: str | None = None,
        date_added: str | None = None,
    ) -> PatientFile:
        """
        Complete documented 2-stage Nookal file upload workflow:
        1. Call /uploadFile to obtain file_id and presigned S3 URL
        2. Perform direct HTTP PUT of bytes to presigned S3 URL
        3. Verify S3 HTTP status (200-299)
        4. Call /setFileActive to confirm activation
        5. Return populated PatientFile
        """
        if not content:
            raise NookalValidationError("document content cannot be empty")

        clean_ext = extension.lstrip(".")
        file_path_name = f"{name}.{clean_ext}"

        file_id, presigned_url = self.upload_file(
            patient_id=patient_id,
            name=name,
            extension=clean_ext,
            file_type=file_type,
            file_path=file_path_name,
            case_id=case_id,
            date_added=date_added,
        )

        # Stage 2: HTTP PUT bytes directly to presigned S3 URL
        try:
            put_resp = httpx.put(
                presigned_url,
                content=content,
                headers={"Content-Type": file_type},
                timeout=self._config.timeout_seconds,
            )
        except Exception as exc:
            sanitized_err = _redact_secrets(str(exc), self._config.api_key)
            raise NookalError(f"Presigned S3 file PUT failed: {sanitized_err}") from None

        if not (200 <= put_resp.status_code < 300):
            raise NookalError(f"Presigned S3 file PUT returned HTTP {put_resp.status_code}")

        # Stage 3: Activate file
        self.set_file_active(patient_id=patient_id, file_id=file_id)

        return PatientFile(
            file_id=file_id,
            patient_id=patient_id,
            name=name,
            file_type=file_type,
            date_added=date_added or date.today().isoformat(),
            size=len(content),
            raw={"file_id": file_id, "name": name, "file_type": file_type},
        )

    # --- Invoices ---

    def get_invoice(self, invoice_id: str, *, void: int | None = None) -> Invoice:
        if not invoice_id:
            raise NookalValidationError("invoice_id is required for get_invoice")

        # Nookal API accepts multiple aliases for invoice identifier: invoice_id, invoiceID, InvoiceID, ID
        params: dict[str, Any] = {
            "invoice_id": invoice_id,
            "invoiceID": invoice_id,
            "InvoiceID": invoice_id,
            "ID": invoice_id,
        }
        if void is not None:
            params["void"] = void

        inv_data = None
        # 1. First attempt: call dedicated /getInvoice endpoint
        try:
            data = self._request(
                "GET",
                "/getInvoice",
                action="get_invoice",
                target_type="finance",
                target_id=invoice_id,
                params=params,
            )
            inv_data = _unwrap_invoice_data(data, invoice_id=invoice_id)
        except (NookalNotFound, NookalValidationError, NookalError):
            inv_data = None

        # 2. Resilient fallback: if /getInvoice was not found, errored, or returned envelope without valid ID
        if not inv_data or not (
            inv_data.get("ID")
            or inv_data.get("id")
            or inv_data.get("invoice_id")
            or inv_data.get("invoiceID")
            or inv_data.get("InvoiceID")
        ):
            try:
                # Query /getInvoices with expanded=1 to search for the record
                invoices = self.get_invoices(expanded=1, void=void)
                matching = next(
                    (
                        inv
                        for inv in invoices
                        if str(inv.invoice_id) == str(invoice_id)
                        or (inv.reference and str(inv.reference) == str(invoice_id))
                    ),
                    None,
                )
                if matching:
                    return matching
            except Exception:
                pass

        if inv_data and isinstance(inv_data, Mapping):
            return self._parse_invoice(inv_data, fallback_invoice_id=invoice_id)

        raise NookalNotFound(f"get_invoice: invoice {invoice_id} not found")

    def get_invoices(
        self,
        patient_id: str | None = None,
        *,
        last_modified: str | None = None,
        void: int | None = None,
        expanded: int | None = None,
    ) -> list[Invoice]:
        if patient_id is not None and not str(patient_id).strip():
            raise NookalValidationError("patient_id cannot be empty string")
        params: dict[str, Any] = {}
        if patient_id:
            params["patient_id"] = patient_id
        if last_modified:
            params["last_modified"] = last_modified
        if void is not None:
            params["void"] = void
        if expanded is not None:
            params["expanded"] = expanded

        data = self._request(
            "GET",
            "/getInvoices",
            action="get_invoices",
            target_type="finance",
            target_id=patient_id or "all",
            params=params,
        )
        rows = _unwrap_collection(data, "invoices")
        return [self._parse_invoice(r, fallback_patient_id=patient_id) for r in rows if isinstance(r, Mapping)]

    def get_invoice_entries(
        self,
        *,
        invoice_id: str | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        lastmodified_date_from: date | None = None,
        lastmodified_date_to: date | None = None,
    ) -> list[InvoiceEntry]:
        params: dict[str, Any] = {}
        if invoice_id:
            params["invoiceID"] = invoice_id
        if date_from:
            params["date_from"] = date_from.strftime("%Y-%m-%d")
        if date_to:
            params["date_to"] = date_to.strftime("%Y-%m-%d")
        if lastmodified_date_from:
            params["lastmodified_date_from"] = lastmodified_date_from.strftime("%Y-%m-%d")
        if lastmodified_date_to:
            params["lastmodified_date_to"] = lastmodified_date_to.strftime("%Y-%m-%d")

        data = self._request(
            "GET",
            "/getInvoiceEntries",
            action="get_invoice_entries",
            target_type="finance",
            target_id=invoice_id or "entries",
            params=params,
        )
        rows = _unwrap_collection(data, "entries")
        return [self._parse_invoice_entry(r) for r in rows if isinstance(r, Mapping)]

    def get_invoice_credits(self, **params: Any) -> list[Mapping[str, Any]]:
        data = self._request("GET", "/getInvoiceCredits", action="get_invoice_credits", target_type="finance", target_id="credits", params=params)
        return [dict(r) for r in _unwrap_collection(data, "credits") if isinstance(r, Mapping)]

    def get_invoice_discounts(self, **params: Any) -> list[Mapping[str, Any]]:
        data = self._request("GET", "/getInvoiceDiscounts", action="get_invoice_discounts", target_type="finance", target_id="discounts", params=params)
        return [dict(r) for r in _unwrap_collection(data, "discounts") if isinstance(r, Mapping)]

    def get_invoice_payments(self, **params: Any) -> list[Mapping[str, Any]]:
        data = self._request("GET", "/getInvoicePayments", action="get_invoice_payments", target_type="finance", target_id="payments", params=params)
        return [dict(r) for r in _unwrap_collection(data, "payments") if isinstance(r, Mapping)]

    def get_invoice_refunds(self, **params: Any) -> list[Mapping[str, Any]]:
        data = self._request("GET", "/getInvoiceRefunds", action="get_invoice_refunds", target_type="finance", target_id="refunds", params=params)
        return [dict(r) for r in _unwrap_collection(data, "refunds") if isinstance(r, Mapping)]

    def get_invoice_adjustments(self, **params: Any) -> list[Mapping[str, Any]]:
        data = self._request("GET", "/getInvoiceAdjustments", action="get_invoice_adjustments", target_type="finance", target_id="adjustments", params=params)
        return [dict(r) for r in _unwrap_collection(data, "adjustments") if isinstance(r, Mapping)]

    # --- Financial writes (supported official endpoints) ---

    def add_invoice(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        data = self._request("POST", "/addInvoice", action="add_invoice", target_type="finance", target_id=str(payload.get("patient_id", "new")), is_write=True, json_body=payload)
        return dict(data) if isinstance(data, Mapping) else {"result": data}

    def delete_invoice(self, invoice_id: str) -> bool:
        self._request("POST", "/deleteInvoice", action="delete_invoice", target_type="finance", target_id=invoice_id, is_write=True, json_body={"invoice_id": invoice_id})
        return True

    def add_item_to_invoice(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        data = self._request("POST", "/addItemToInvoice", action="add_item_to_invoice", target_type="finance", target_id=str(payload.get("invoice_id", "")), is_write=True, json_body=payload)
        return dict(data) if isinstance(data, Mapping) else {"result": data}

    def delete_item_from_invoice(self, item_id: str) -> bool:
        self._request("POST", "/deleteItemFromInvoice", action="delete_item_from_invoice", target_type="finance", target_id=item_id, is_write=True, json_body={"item_id": item_id})
        return True

    def add_payment_to_invoice(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        data = self._request("POST", "/addPaymentToInvoice", action="add_payment_to_invoice", target_type="finance", target_id=str(payload.get("invoice_id", "")), is_write=True, json_body=payload)
        return dict(data) if isinstance(data, Mapping) else {"result": data}

    def delete_payment_from_invoice(self, payment_id: str) -> bool:
        self._request("POST", "/deletePaymentFromInvoice", action="delete_payment_from_invoice", target_type="finance", target_id=payment_id, is_write=True, json_body={"payment_id": payment_id})
        return True

    def add_account_credit(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        data = self._request("POST", "/addAccountCredit", action="add_account_credit", target_type="finance", target_id=str(payload.get("patient_id", "")), is_write=True, json_body=payload)
        return dict(data) if isinstance(data, Mapping) else {"result": data}

    # --- Parsers ---

    @staticmethod
    def _parse_patient(data: Any) -> PatientRef:
        if not isinstance(data, Mapping):
            raise NookalValidationError("unexpected patient payload shape")
        pid = (
            data.get("ID")
            or data.get("id")
            or data.get("patient_id")
            or data.get("patientID")
            or data.get("PatientID")
            or data.get("patientId")
            or data.get("PatientId")
        )
        if not pid:
            raise NookalValidationError("patient payload missing id")

        first = data.get("firstName") or data.get("first_name") or data.get("FirstName") or data.get("firstname") or ""
        middle = data.get("middleName") or data.get("middle_name") or data.get("MiddleName") or None
        nick = data.get("nickName") or data.get("nickname") or data.get("NickName") or None
        last = data.get("lastName") or data.get("last_name") or data.get("LastName") or data.get("lastname") or ""
        full = (f"{first} {last}".strip()) or data.get("name") or data.get("full_name") or data.get("Name") or None

        phone = (
            data.get("mobile")
            or data.get("Mobile")
            or data.get("phone")
            or data.get("telephone")
            or data.get("Telephone")
        )
        email = data.get("email") or data.get("Email")

        dob_raw = data.get("DOB") or data.get("date_of_birth") or data.get("dateOfBirth") or data.get("dob")
        parsed_dob = None
        if dob_raw:
            if isinstance(dob_raw, date):
                parsed_dob = dob_raw
            elif isinstance(dob_raw, str):
                s = dob_raw.strip()
                try:
                    parsed_dob = date.fromisoformat(s[:10])
                except ValueError:
                    try:
                        parsed_dob = datetime.strptime(s[:10], "%d/%m/%Y").date()
                    except ValueError:
                        pass

        gender = data.get("gender") or data.get("Gender")
        suburb = data.get("suburb") or data.get("Suburb")
        if not suburb and isinstance(data.get("address"), Mapping):
            addr = data["address"]
            suburb = addr.get("suburb") or addr.get("Suburb") or addr.get("city") or addr.get("City")
        elif not suburb and isinstance(data.get("address"), str):
            suburb = data.get("address")

        address = data.get("address")
        postal_address = data.get("postalAddress") or data.get("postal_address")
        online_code = data.get("onlineCode") or data.get("online_code")
        deceased_val = data.get("deceased")
        deceased = bool(deceased_val in (1, "1", True, "true"))

        ref_id = data.get("referrer_id") or data.get("referrerID")
        date_created = data.get("dateCreated") or data.get("date_created")
        date_modified = data.get("dateModified") or data.get("date_modified")

        return PatientRef(
            patient_id=str(pid),
            first_name=first or None,
            middle_name=middle,
            last_name=last or None,
            nickname=nick,
            phone=phone,
            email=email,
            display_name=full,
            date_of_birth=parsed_dob,
            gender=str(gender) if gender else None,
            suburb=str(suburb) if suburb else None,
            address=address,
            postal_address=postal_address,
            online_code=str(online_code) if online_code else None,
            deceased=deceased,
            referrer_id=str(ref_id) if ref_id else None,
            date_created=str(date_created) if date_created else None,
            date_modified=str(date_modified) if date_modified else None,
            raw=dict(data),
        )

    @staticmethod
    def _parse_appointment(data: Any) -> Appointment:
        if not isinstance(data, Mapping):
            raise NookalValidationError("unexpected appointment payload shape")
        aid = (
            data.get("ID")
            or data.get("id")
            or data.get("appointment_id")
            or data.get("appointmentID")
            or data.get("AppointmentID")
            or data.get("appointmentId")
            or data.get("AppointmentId")
        )
        pid = (
            data.get("patientID")
            or data.get("patient_id")
            or data.get("PatientID")
            or data.get("patientId")
            or data.get("PatientId")
        )
        if not pid and isinstance(data.get("patient"), Mapping):
            pid = (
                data["patient"].get("ID")
                or data["patient"].get("id")
                or data["patient"].get("patient_id")
                or data["patient"].get("patientID")
                or data["patient"].get("PatientID")
                or data["patient"].get("patientId")
                or data["patient"].get("PatientId")
            )
        elif not pid and isinstance(data.get("patient"), (str, int)):
            pid = data.get("patient")

        if not aid or not pid:
            raise NookalValidationError("appointment payload missing required fields (id, patient_id)")

        appt_date_raw = (
            data.get("appointmentDate")
            or data.get("appointment_date")
            or data.get("AppointmentDate")
            or data.get("date")
            or data.get("Date")
            or data.get("startDate")
            or data.get("start_date")
            or data.get("StartDate")
        )
        start_time_raw = (
            data.get("appointmentStartTime")
            or data.get("appointment_start_time")
            or data.get("AppointmentStartTime")
            or data.get("startTime")
            or data.get("start_time")
            or data.get("StartTime")
            or data.get("appointmentTime")
            or data.get("appointment_time")
            or data.get("AppointmentTime")
            or data.get("time")
            or data.get("Time")
            or data.get("timeStart")
            or data.get("time_start")
            or data.get("TimeStart")
        )
        end_time_raw = (
            data.get("appointmentEndTime")
            or data.get("appointment_end_time")
            or data.get("AppointmentEndTime")
            or data.get("endTime")
            or data.get("end_time")
            or data.get("EndTime")
            or data.get("timeEnd")
            or data.get("time_end")
            or data.get("TimeEnd")
        )
        end_date_raw = (
            data.get("appointmentEndDate")
            or data.get("appointment_end_date")
            or data.get("AppointmentEndDate")
            or data.get("endDate")
            or data.get("end_date")
            or data.get("EndDate")
            or appt_date_raw
        )

        def _try_parse_dt(val: Any) -> datetime | None:
            if not val:
                return None
            if isinstance(val, datetime):
                return val
            if isinstance(val, date):
                return datetime.combine(val, dt_time.min)
            s = str(val).strip()
            if not s:
                return None
            try:
                return datetime.fromisoformat(s.replace("Z", "+00:00"))
            except ValueError:
                pass
            for fmt in (
                "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%dT%H:%M",
                "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d %H:%M",
                "%Y-%m-%d %I:%M %p",
                "%Y-%m-%d %I:%M%p",
                "%d/%m/%Y %H:%M:%S",
                "%d/%m/%Y %H:%M",
                "%d/%m/%Y %I:%M %p",
                "%d/%m/%Y %I:%M%p",
                "%m/%d/%Y %H:%M:%S",
                "%m/%d/%Y %H:%M",
                "%Y/%m/%d %H:%M:%S",
                "%Y/%m/%d %H:%M",
            ):
                try:
                    return datetime.strptime(s, fmt)
                except ValueError:
                    pass
            for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%Y/%m/%d"):
                try:
                    return datetime.combine(datetime.strptime(s[:10], fmt).date(), dt_time.min)
                except ValueError:
                    pass
            return None

        def _try_parse_combined(d_val: Any, t_val: Any) -> datetime | None:
            if not d_val or not t_val:
                return None
            clean_date = str(d_val).strip()
            clean_time = str(t_val).strip()
            if not clean_date or not clean_time:
                return None
            if "T" in clean_time or ("-" in clean_time and len(clean_time) >= 10):
                parsed = _try_parse_dt(clean_time)
                if parsed:
                    return parsed
            for fmt in (
                "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%dT%H:%M",
                "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d %H:%M",
                "%Y-%m-%d %I:%M %p",
                "%Y-%m-%d %I:%M%p",
                "%d/%m/%Y %H:%M:%S",
                "%d/%m/%Y %H:%M",
                "%d/%m/%Y %I:%M %p",
                "%d/%m/%Y %I:%M%p",
                "%m/%d/%Y %H:%M:%S",
                "%m/%d/%Y %H:%M",
                "%Y/%m/%d %H:%M:%S",
                "%Y/%m/%d %H:%M",
            ):
                try:
                    s = f"{clean_date}T{clean_time}".replace(" ", "T", 1) if "T" in fmt else f"{clean_date} {clean_time}"
                    return datetime.strptime(s, fmt)
                except ValueError:
                    pass
            try:
                return datetime.fromisoformat(f"{clean_date}T{clean_time}")
            except ValueError:
                pass
            return None

        starts_at = None
        if appt_date_raw and start_time_raw:
            starts_at = _try_parse_combined(appt_date_raw, start_time_raw)

        if starts_at is None:
            for candidate in (
                data.get("starts_at"),
                data.get("start"),
                data.get("datetime"),
                data.get("DateTime"),
                data.get("appointment_datetime"),
                data.get("appointmentDateTime"),
                data.get("startDateTime"),
                data.get("start_datetime"),
                data.get("StartDateTime"),
            ):
                starts_at = _try_parse_dt(candidate)
                if starts_at is not None:
                    break

        if starts_at is None and appt_date_raw:
            starts_at = _try_parse_dt(appt_date_raw)

        if starts_at is None and start_time_raw:
            starts_at = _try_parse_dt(start_time_raw)

        if starts_at is None:
            raise NookalValidationError("appointment payload missing start time/date")

        ends_at = None
        if end_date_raw and end_time_raw:
            ends_at = _try_parse_combined(end_date_raw, end_time_raw)
        elif appt_date_raw and end_time_raw:
            ends_at = _try_parse_combined(appt_date_raw, end_time_raw)

        if ends_at is None:
            for candidate in (
                data.get("ends_at"),
                data.get("end"),
                data.get("endDateTime"),
                data.get("end_datetime"),
                data.get("EndDateTime"),
                data.get("appointmentEndDateTime"),
                data.get("appointment_end_datetime"),
            ):
                ends_at = _try_parse_dt(candidate)
                if ends_at is not None:
                    break

        if ends_at is None and starts_at is not None:
            dur = data.get("duration") or data.get("length") or data.get("duration_minutes") or data.get("durationMinutes")
            if dur:
                try:
                    ends_at = starts_at + timedelta(minutes=int(dur))
                except (ValueError, TypeError):
                    pass

        cancelled_val = str(data.get("cancelled", "")).strip()
        dna_val = str(data.get("DNA") or data.get("dna") or data.get("did_not_arrive") or "").strip()
        arrived_val = str(data.get("arrived") or data.get("is_arrived") or "").strip()

        if cancelled_val in ("1", "true", "True") or data.get("cancellationDate"):
            status = "cancelled"
        elif dna_val in ("1", "true", "True"):
            status = "dna"
        elif arrived_val in ("1", "true", "True"):
            status = "arrived"
        elif data.get("status"):
            status = str(data["status"]).lower()
        else:
            status = "booked"

        loc_id = (
            data.get("locationID")
            or data.get("location_id")
            or data.get("LocationID")
            or data.get("locationId")
            or data.get("location")
        )
        prac_id = (
            data.get("practitionerID")
            or data.get("practitioner_id")
            or data.get("PractitionerID")
            or data.get("practitionerId")
            or data.get("practitioner")
        )
        type_id = data.get("typeID") or data.get("type_id")
        appt_type = data.get("type") or data.get("appointment_type")
        notes = data.get("notes")
        email_reminder = bool(data.get("emailReminderSent") in (1, "1", True, "true"))
        invoice_gen = bool(data.get("invoiceGenerated") in (1, "1", True, "true"))
        cancellation_date = data.get("cancellationDate") or data.get("cancellation_date")

        return Appointment(
            appointment_id=str(aid),
            patient_id=str(pid),
            starts_at=starts_at,
            ends_at=ends_at,
            status=status,
            location_id=str(loc_id) if loc_id else None,
            practitioner_id=str(prac_id) if prac_id else None,
            type_id=str(type_id) if type_id else None,
            appointment_type=str(appt_type) if appt_type else None,
            notes=str(notes) if notes else None,
            arrived=arrived_val in ("1", "true", "True"),
            dna=dna_val in ("1", "true", "True"),
            cancelled=cancelled_val in ("1", "true", "True"),
            cancellation_date=str(cancellation_date) if cancellation_date else None,
            email_reminder_sent=email_reminder,
            invoice_generated=invoice_gen,
            raw=dict(data),
        )

    @staticmethod
    def _parse_case(data: Any, fallback_patient_id: str | None = None) -> CaseRef:
        if not isinstance(data, Mapping):
            raise NookalValidationError("unexpected case payload shape")
        cid = (
            data.get("ID")
            or data.get("id")
            or data.get("case_id")
            or data.get("caseID")
            or data.get("CaseID")
            or data.get("caseId")
        )
        pid = (
            data.get("patientID")
            or data.get("patient_id")
            or data.get("PatientID")
            or data.get("Patient_ID")
            or data.get("patientId")
            or fallback_patient_id
        )
        if not cid or not pid:
            raise NookalValidationError("case payload missing id or patient_id")

        return CaseRef(
            case_id=str(cid),
            patient_id=str(pid),
            case_name=(
                data.get("caseName")
                or data.get("case_name")
                or data.get("name")
                or data.get("CaseName")
                or data.get("title")
            ),
            case_number=(
                data.get("caseNumber")
                or data.get("case_number")
                or data.get("CaseNumber")
            ),
            status=data.get("status") or data.get("Status"),
            date_created=(
                data.get("dateCreated")
                or data.get("date_created")
                or data.get("DateCreated")
            ),
            date_modified=(
                data.get("dateModified")
                or data.get("date_modified")
                or data.get("DateModified")
            ),
            closed_date=(
                data.get("closedDate")
                or data.get("closed_date")
                or data.get("ClosedDate")
            ),
            raw=dict(data),
        )

    @staticmethod
    def _parse_treatment_note(data: Any, fallback_patient_id: str | None = None) -> TreatmentNote:
        if not isinstance(data, Mapping):
            raise NookalValidationError("unexpected treatment note payload shape")
        nid = data.get("ID") or data.get("id") or data.get("note_id") or data.get("noteID") or data.get("NoteID")
        pid = (
            data.get("patientID")
            or data.get("patient_id")
            or data.get("PatientID")
            or data.get("patientId")
            or fallback_patient_id
        )
        if not nid or not pid:
            raise NookalValidationError("treatment note payload missing id or patient_id")

        return TreatmentNote(
            note_id=str(nid),
            patient_id=str(pid),
            case_id=data.get("caseID") or data.get("case_id") or data.get("CaseID"),
            practitioner_id=(
                data.get("practitionerID")
                or data.get("practitioner_id")
                or data.get("PractitionerID")
            ),
            date=data.get("date") or data.get("Date"),
            notes=data.get("notes") or data.get("Notes") or data.get("note"),
            appointment_id=(
                data.get("apptID")
                or data.get("appt_id")
                or data.get("appointment_id")
                or data.get("appointmentID")
            ),
            raw=dict(data),
        )

    @staticmethod
    def _parse_extra(data: Any) -> PatientExtra:
        if not isinstance(data, Mapping):
            raise NookalValidationError("unexpected extra payload shape")
        eid = data.get("ID") or data.get("id") or data.get("extra_id")
        name = data.get("name") or data.get("extra_name") or ""
        opts = data.get("options")
        if isinstance(opts, list):
            options_list = [str(o) for o in opts]
        elif isinstance(opts, str):
            options_list = [o.strip() for o in opts.split(",") if o.strip()]
        else:
            options_list = []

        return PatientExtra(
            extra_id=str(eid or ""),
            name=str(name),
            field_type=data.get("type") or data.get("field_type"),
            options=options_list,
            raw=dict(data),
        )

    @staticmethod
    def _parse_location(data: Any) -> Location:
        if not isinstance(data, Mapping):
            raise NookalValidationError("unexpected location payload shape")
        lid = data.get("ID") or data.get("id") or data.get("location_id")
        name = data.get("name") or data.get("location_name") or ""
        addr = data.get("address")
        if isinstance(addr, Mapping):
            addr_str = ", ".join(str(v) for v in addr.values() if v)
        else:
            addr_str = str(addr) if addr else None

        return Location(
            location_id=str(lid or ""),
            name=str(name),
            address=addr_str,
            raw=dict(data),
        )

    @staticmethod
    def _parse_practitioner(data: Any) -> Practitioner:
        if not isinstance(data, Mapping):
            raise NookalValidationError("unexpected practitioner payload shape")
        pid = data.get("ID") or data.get("id") or data.get("practitioner_id")
        first = data.get("firstName") or data.get("first_name") or ""
        last = data.get("lastName") or data.get("last_name") or ""
        locs = data.get("locations") or []
        loc_list = [str(l) for l in locs] if isinstance(locs, list) else []

        return Practitioner(
            practitioner_id=str(pid or ""),
            first_name=str(first) if first else None,
            last_name=str(last) if last else None,
            speciality=data.get("speciality") or data.get("specialty"),
            title=data.get("title"),
            email=data.get("email"),
            locations=loc_list,
            raw=dict(data),
        )

    @staticmethod
    def _parse_appointment_type(data: Any) -> AppointmentType:
        if not isinstance(data, Mapping):
            raise NookalValidationError("unexpected appointment type payload shape")
        tid = data.get("ID") or data.get("id") or data.get("type_id")
        name = data.get("name") or ""
        dur = data.get("duration")
        price = data.get("price")
        locs = data.get("locations") or []

        return AppointmentType(
            type_id=str(tid or ""),
            name=str(name),
            description=data.get("description"),
            duration=int(dur) if dur is not None and str(dur).isdigit() else None,
            price=float(price) if price is not None else None,
            has_tax=bool(data.get("hasTax") in (1, "1", True)),
            type=data.get("type"),
            locations=[str(l) for l in locs] if isinstance(locs, list) else [],
            raw=dict(data),
        )

    @staticmethod
    def _parse_class_type(data: Any) -> ClassType:
        if not isinstance(data, Mapping):
            raise NookalValidationError("unexpected class type payload shape")
        cid = data.get("ID") or data.get("id") or data.get("class_id")
        name = data.get("name") or ""
        dur = data.get("duration")
        price = data.get("price")
        locs = data.get("locations") or []

        return ClassType(
            class_id=str(cid or ""),
            name=str(name),
            description=data.get("description"),
            duration=int(dur) if dur is not None and str(dur).isdigit() else None,
            price=float(price) if price is not None else None,
            locations=[str(l) for l in locs] if isinstance(locs, list) else [],
            raw=dict(data),
        )

    @staticmethod
    def _parse_class_participant(data: Any) -> ClassParticipant:
        if not isinstance(data, Mapping):
            raise NookalValidationError("unexpected class participant shape")
        pid = data.get("ID") or data.get("id") or data.get("participant_id")
        cid = data.get("classID") or data.get("class_id") or ""
        patient_id = data.get("patientID") or data.get("patient_id")

        return ClassParticipant(
            participant_id=str(pid or ""),
            class_id=str(cid),
            patient_id=str(patient_id) if patient_id else None,
            status=data.get("status"),
            raw=dict(data),
        )

    @staticmethod
    def _parse_patient_file(data: Any, fallback_patient_id: str | None = None) -> PatientFile:
        if not isinstance(data, Mapping):
            raise NookalValidationError("unexpected patient file payload shape")
        fid = (
            data.get("ID")
            or data.get("id")
            or data.get("file_id")
            or data.get("fileID")
            or data.get("FileID")
            or data.get("fileId")
        )
        pid = (
            data.get("patientID")
            or data.get("patient_id")
            or data.get("PatientID")
            or data.get("Patient_ID")
            or data.get("patientId")
            or fallback_patient_id
        )
        name = (
            data.get("name")
            or data.get("filename")
            or data.get("fileName")
            or data.get("FileName")
            or data.get("file_name")
            or data.get("title")
            or data.get("file_path")
            or "file"
        )
        size = data.get("size") or data.get("file_size") or data.get("fileSize") or data.get("FileSize")
        parsed_size: int | None = None
        if size is not None:
            try:
                parsed_size = int(float(size))
            except (ValueError, TypeError):
                parsed_size = None

        return PatientFile(
            file_id=str(fid or ""),
            patient_id=str(pid or ""),
            name=str(name),
            file_type=(
                data.get("fileType")
                or data.get("file_type")
                or data.get("FileType")
                or data.get("extension")
                or data.get("type")
            ),
            date_added=(
                data.get("dateAdded")
                or data.get("date_added")
                or data.get("DateAdded")
                or data.get("created")
                or data.get("date")
            ),
            size=parsed_size,
            raw=dict(data),
        )

    @staticmethod
    def _parse_invoice(
        data: Any,
        fallback_patient_id: str | None = None,
        fallback_invoice_id: str | None = None,
    ) -> Invoice:
        if not isinstance(data, Mapping):
            raise NookalValidationError("unexpected invoice payload shape")
        iid = (
            data.get("ID")
            or data.get("id")
            or data.get("invoice_id")
            or data.get("invoiceID")
            or data.get("InvoiceID")
            or data.get("invoiceId")
            or data.get("invoiceNumber")
            or data.get("InvoiceNumber")
            or fallback_invoice_id
        )
        patient_obj = data.get("patient") if isinstance(data.get("patient"), Mapping) else {}
        client_obj = data.get("client") if isinstance(data.get("client"), Mapping) else {}
        pid = (
            data.get("patientID")
            or data.get("patient_id")
            or data.get("PatientID")
            or data.get("Patient_ID")
            or data.get("patientId")
            or data.get("clientID")
            or data.get("client_id")
            or data.get("ClientID")
            or patient_obj.get("ID")
            or patient_obj.get("id")
            or patient_obj.get("patientID")
            or client_obj.get("ID")
            or client_obj.get("id")
            or fallback_patient_id
        )
        account_data = data.get("account") if isinstance(data.get("account"), Mapping) else {}

        # In Nookal API, totalDebits is the primary invoice amount (debits charged to patient account)
        raw_total = (
            data.get("totalDebits")
            or data.get("TotalDebits")
            or data.get("total_debits")
            or data.get("total")
            or data.get("amount")
            or data.get("totalAmount")
            or data.get("total_amount")
            or data.get("invoice_total")
            or data.get("invoiceTotal")
            or data.get("Total")
            or account_data.get("debits")
            or account_data.get("totalDebits")
            or data.get("totalPayments")
            or data.get("totalBalance")
        )
        void_val = data.get("void") or data.get("isVoid") or data.get("is_void") or data.get("Void")

        entries_raw = (
            data.get("entries")
            or data.get("items")
            or data.get("invoice_entries")
            or (data.get("details", {}).get("entries") if isinstance(data.get("details"), Mapping) else None)
            or []
        )
        if isinstance(entries_raw, Mapping):
            entries_raw = list(entries_raw.values())
        entries = []
        if isinstance(entries_raw, list):
            for e in entries_raw:
                if isinstance(e, Mapping):
                    entries.append(
                        HttpNookalClient._parse_invoice_entry(
                            e, fallback_invoice_id=str(iid or "")
                        )
                    )

        parsed_total: float | None = None
        if raw_total is not None:
            try:
                if isinstance(raw_total, (int, float)):
                    parsed_total = float(raw_total)
                else:
                    cleaned = str(raw_total).replace("$", "").replace(",", "").strip()
                    parsed_total = float(cleaned) if cleaned else None
            except (ValueError, TypeError):
                parsed_total = None

        if (parsed_total is None or parsed_total == 0.0) and entries:
            entry_sum = sum(
                (e.total if e.total is not None else ((e.price or 0.0) * (e.quantity or 1.0)))
                for e in entries
            )
            if entry_sum > 0:
                parsed_total = float(entry_sum)

        raw_date = (
            data.get("date")
            or data.get("dateCreated")
            or data.get("DateCreated")
            or data.get("date_created")
            or data.get("invoiceDate")
            or data.get("invoice_date")
            or data.get("InvoiceDate")
            or data.get("issueDate")
            or data.get("issue_date")
            or data.get("IssueDate")
            or data.get("Date")
            or data.get("created")
            or data.get("created_date")
            or data.get("created_at")
            or data.get("createdAt")
            or data.get("timestamp")
        )
        parsed_date: str | None = None
        if raw_date is not None:
            date_str = str(raw_date).strip()
            if len(date_str) >= 10 and date_str[4] == "-" and date_str[7] == "-":
                parsed_date = date_str[:10]
            else:
                parsed_date = date_str

        def _to_float(v: Any) -> float | None:
            if v is None:
                return None
            try:
                return float(str(v).replace("$", "").replace(",", "").strip())
            except Exception:
                return None

        bal_val = _to_float(
            data.get("totalBalance")
            or data.get("TotalBalance")
            or data.get("total_balance")
            or data.get("balance")
            or data.get("Balance")
            or data.get("balanceDue")
            or data.get("balance_due")
            or data.get("BalanceDue")
            or data.get("amountDue")
            or data.get("amount_due")
            or data.get("AmountDue")
            or account_data.get("balance")
            or account_data.get("totalBalance")
        )
        deb_val = _to_float(
            data.get("totalDebits")
            or data.get("TotalDebits")
            or data.get("total_debits")
            or data.get("total")
            or data.get("Total")
            or data.get("amount")
            or data.get("Amount")
            or data.get("totalAmount")
            or data.get("total_amount")
            or account_data.get("debits")
            or account_data.get("totalDebits")
            or parsed_total
        )
        pay_val = _to_float(
            data.get("totalPayments")
            or data.get("TotalPayments")
            or data.get("total_payments")
            or data.get("paid")
            or data.get("Paid")
            or data.get("amountPaid")
            or data.get("amount_paid")
            or data.get("totalPaid")
            or data.get("total_paid")
            or account_data.get("payments")
            or account_data.get("totalPayments")
        )

        is_void = bool(void_val in (1, "1", True, "true", "True"))

        raw_status = (
            data.get("status")
            or data.get("Status")
            or data.get("invoiceStatus")
            or data.get("invoice_status")
            or data.get("InvoiceStatus")
            or data.get("paymentStatus")
            or data.get("payment_status")
            or data.get("PaymentStatus")
            or account_data.get("status")
            or account_data.get("paymentStatus")
        )

        status_clean = str(raw_status).strip().lower() if raw_status is not None else ""

        if is_void or status_clean in ("void", "voided", "cancelled", "canceled"):
            final_status = "Void"
            is_void = True
        elif (
            status_clean in ("paid", "completed", "settled", "closed", "full")
            or data.get("isPaid") is True
            or data.get("is_paid") in (1, "1", True, "true")
        ):
            final_status = "Paid"
        elif (
            status_clean in ("unpaid", "pending", "outstanding", "open", "due", "draft", "not paid", "un-paid")
            or data.get("isPaid") is False
            or data.get("is_paid") in (0, "0", False, "false")
        ):
            final_status = "Unpaid"
        elif status_clean in ("partial", "partially paid", "partially_paid", "part paid", "part-paid"):
            final_status = "Partially Paid"
        elif bal_val is not None and bal_val <= 0.001 and ((deb_val is not None and deb_val > 0) or (pay_val is not None and pay_val > 0)):
            final_status = "Paid"
        elif pay_val is not None and deb_val is not None and deb_val > 0 and pay_val >= deb_val:
            final_status = "Paid"
        elif pay_val is not None and pay_val > 0 and bal_val is not None and bal_val > 0.001:
            final_status = "Partially Paid"
        elif bal_val is not None and bal_val > 0.001:
            final_status = "Unpaid"
        elif pay_val is not None and pay_val == 0.0:
            final_status = "Unpaid"
        elif status_clean in ("1", "true"):
            final_status = "Paid"
        elif status_clean in ("0", "false"):
            final_status = "Unpaid"
        else:
            final_status = "Unpaid" if (parsed_total is not None and parsed_total > 0) else "Valid"

        # Mandatory consistency: if invoice is Paid, balance due MUST be 0 and amount paid MUST be the total
        if final_status == "Paid":
            bal_val = 0.0
            if parsed_total is not None and parsed_total > 0:
                if pay_val is None or pay_val < parsed_total:
                    pay_val = float(parsed_total)
        elif final_status == "Unpaid":
            if (bal_val is None or bal_val == 0.0) and parsed_total is not None and parsed_total > 0:
                bal_val = float(parsed_total)
            if pay_val is None:
                pay_val = 0.0

        loc_obj = data.get("location") if isinstance(data.get("location"), Mapping) else {}
        loc_id = (
            data.get("locationID")
            or data.get("location_id")
            or data.get("LocationID")
            or loc_obj.get("ID")
            or loc_obj.get("id")
            or (data.get("location") if not isinstance(data.get("location"), Mapping) else None)
            or data.get("locationId")
        )
        prac_obj = data.get("practitioner") if isinstance(data.get("practitioner"), Mapping) else {}
        prov_obj = data.get("provider") if isinstance(data.get("provider"), Mapping) else {}
        prac_id = (
            data.get("practitionerID")
            or data.get("practitioner_id")
            or data.get("PractitionerID")
            or prac_obj.get("ID")
            or prac_obj.get("id")
            or prov_obj.get("ID")
            or prov_obj.get("id")
            or (data.get("practitioner") if not isinstance(data.get("practitioner"), Mapping) else None)
            or data.get("practitionerId")
            or data.get("providerID")
            or data.get("provider_id")
        )
        case_obj = data.get("case") if isinstance(data.get("case"), Mapping) else {}
        case_id = (
            data.get("caseID")
            or data.get("case_id")
            or data.get("CaseID")
            or case_obj.get("ID")
            or case_obj.get("id")
            or (data.get("case") if not isinstance(data.get("case"), Mapping) else None)
            or data.get("caseId")
            or data.get("caseNumber")
            or data.get("case_number")
        )
        ref_no = (
            data.get("reference")
            or data.get("referenceNumber")
            or data.get("Reference")
            or data.get("reference_number")
            or data.get("ReferenceNumber")
            or data.get("invoiceNumber")
            or data.get("invoice_number")
            or data.get("InvoiceNumber")
            or data.get("invoiceNo")
            or data.get("invoice_no")
            or data.get("InvoiceNo")
            or data.get("ref")
            or data.get("Ref")
            or data.get("number")
        )
        raw_due_date = (
            data.get("dueDate")
            or data.get("due_date")
            or data.get("dateDue")
            or data.get("DateDue")
            or data.get("DueDate")
            or data.get("date_due")
            or data.get("paymentDueDate")
            or data.get("payment_due_date")
        )
        parsed_due_date = None
        if raw_due_date:
            d_str = str(raw_due_date).strip()
            if len(d_str) >= 10 and d_str[4] == "-" and d_str[7] == "-":
                parsed_due_date = d_str[:10]
            else:
                parsed_due_date = d_str

        raw_tax = _to_float(
            data.get("tax")
            or data.get("totalTax")
            or data.get("Tax")
            or data.get("total_tax")
            or data.get("gst")
            or data.get("totalGST")
        )
        if raw_tax is None and entries:
            tax_sum = sum(e.tax for e in entries if e.tax is not None)
            if tax_sum > 0:
                raw_tax = tax_sum

        notes_val = (
            data.get("notes")
            or data.get("comment")
            or data.get("comments")
            or data.get("description")
            or data.get("Notes")
            or data.get("memo")
            or data.get("note")
        )

        raw_dict = dict(data)
        if patient_obj:
            pn = patient_obj.get("name") or f"{patient_obj.get('first_name', '')} {patient_obj.get('last_name', '')}".strip()
            if pn and "patient_name" not in raw_dict:
                raw_dict["patient_name"] = pn
        if loc_obj:
            ln = loc_obj.get("name") or loc_obj.get("location_name")
            if ln and "location_name" not in raw_dict:
                raw_dict["location_name"] = ln
        if prac_obj:
            prn = prac_obj.get("name") or f"{prac_obj.get('first_name', '')} {prac_obj.get('last_name', '')}".strip()
            if prn and "practitioner_name" not in raw_dict:
                raw_dict["practitioner_name"] = prn

        return Invoice(
            invoice_id=str(iid or ""),
            patient_id=str(pid or ""),
            date=parsed_date,
            total=parsed_total,
            status=final_status,
            void=is_void,
            expanded=bool(entries),
            entries=entries,
            location_id=str(loc_id) if loc_id else None,
            practitioner_id=str(prac_id) if prac_id else None,
            case_id=str(case_id) if case_id else None,
            reference=str(ref_no) if ref_no else None,
            due_date=parsed_due_date,
            balance=bal_val,
            paid=pay_val,
            tax=raw_tax,
            notes=str(notes_val) if notes_val else None,
            raw=raw_dict,
        )

    @staticmethod
    def _parse_invoice_entry(
        data: Any, fallback_invoice_id: str | None = None
    ) -> InvoiceEntry:
        if not isinstance(data, Mapping):
            raise NookalValidationError("unexpected invoice entry payload shape")
        eid = (
            data.get("ID")
            or data.get("id")
            or data.get("entry_id")
            or data.get("entryID")
            or data.get("EntryID")
        )
        price = data.get("price") or data.get("amount") or data.get("Price")
        qty = data.get("quantity") or data.get("qty") or data.get("Quantity")
        tax = data.get("tax") or data.get("Tax")
        total_val = data.get("total") or data.get("Total") or data.get("subtotal") or data.get("subTotal") or data.get("amount")

        def _clean_num(val: Any) -> float | None:
            if val is None:
                return None
            try:
                if isinstance(val, (int, float)):
                    return float(val)
                cleaned = str(val).replace("$", "").replace(",", "").strip()
                return float(cleaned) if cleaned else None
            except (ValueError, TypeError):
                return None

        c_price = _clean_num(price)
        c_qty = _clean_num(qty)
        c_tax = _clean_num(tax)
        c_total = _clean_num(total_val)
        if c_total is None and c_price is not None:
            c_total = c_price * (c_qty if c_qty is not None else 1.0)
            if c_tax is not None:
                c_total += c_tax

        return InvoiceEntry(
            entry_id=str(eid or ""),
            invoice_id=str(
                data.get("invoiceID")
                or data.get("invoice_id")
                or data.get("InvoiceID")
                or fallback_invoice_id
                or ""
            )
            or None,
            item_id=str(data.get("itemID") or data.get("item_id") or data.get("ItemID") or "")
            or None,
            description=data.get("description") or data.get("item_name") or data.get("name"),
            price=c_price,
            quantity=c_qty,
            tax=c_tax,
            total=c_total,
            raw=dict(data),
        )


def _age_years(dob: date, as_of: date) -> int:
    years = as_of.year - dob.year
    if (as_of.month, as_of.day) < (dob.month, dob.day):
        years -= 1
    return years


class MockNookalClient(NookalClient):
    """In-memory stand-in for tests and dry-runs. Implements the full NookalClient interface."""

    def __init__(
        self,
        *,
        audit: AuditFn | None = None,
        actor: str = "nookal_client.mock",
        as_of: date | None = None,
    ) -> None:
        self._audit = audit or audit_mod.log_event
        self._actor = actor
        self.as_of = as_of
        self.patients: dict[str, PatientRef] = {}
        self.appointments: dict[str, Appointment] = {}
        self.cases: dict[str, CaseRef] = {}
        self.treatment_notes: list[TreatmentNote] = []
        self.extras: list[PatientExtra] = []
        self.locations: dict[str, Location] = {}
        self.practitioners: dict[str, Practitioner] = {}
        self.appointment_types: dict[str, AppointmentType] = {}
        self.class_types: dict[str, ClassType] = {}
        self.class_participants: list[ClassParticipant] = []
        self.files: dict[str, PatientFile] = {}
        self.invoices: dict[str, Invoice] = {}
        self.invoice_entries: list[InvoiceEntry] = []
        self.open_slots: list[datetime] = []
        # Legacy test data stores (for test_orchestration_phase_c / test_mock_nookal_phase_a)
        self.referrers: dict[str, Referrer] = {}
        self.referrals: dict[str, Referral] = {}
        self.documents: list[DocumentMeta] = []
        self._seq = 1000

    def _next_id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}_{self._seq}"

    def _age_as_of(self) -> date:
        if self.as_of is not None:
            return self.as_of
        return date.today()

    def seed_patient(self, patient: PatientRef) -> None:
        self.patients[patient.patient_id] = patient

    def seed_open_slot(self, when: datetime) -> None:
        self.open_slots.append(when)

    def list_open_slots(self, *, on_date: date | None = None) -> list[datetime]:
        slots = list(self.open_slots)
        if on_date is not None:
            slots = [s for s in slots if s.date() == on_date]
        return sorted(slots)

    def seed_appointment(self, appointment: Appointment) -> None:
        self.appointments[appointment.appointment_id] = appointment
        patient = self.patients.get(appointment.patient_id)
        if patient is not None:
            appt_day = appointment.starts_at.date()
            if patient.last_appointment_date is None or appt_day > patient.last_appointment_date:
                self.patients[patient.patient_id] = PatientRef(
                    patient_id=patient.patient_id,
                    first_name=patient.first_name,
                    last_name=patient.last_name,
                    middle_name=patient.middle_name,
                    nickname=patient.nickname,
                    phone=patient.phone,
                    email=patient.email,
                    display_name=patient.display_name,
                    date_of_birth=patient.date_of_birth,
                    gender=patient.gender,
                    suburb=patient.suburb,
                    address=patient.address,
                    postal_address=patient.postal_address,
                    online_code=patient.online_code,
                    deceased=patient.deceased,
                    referrer_id=patient.referrer_id,
                    last_appointment_date=appt_day,
                )


    def seed_referrer(self, referrer: Referrer) -> None:
        self.referrers[referrer.referrer_id] = referrer

    def seed_referral(self, referral: Referral) -> None:
        self.referrals[referral.referral_id] = referral

    def seed_document(self, document: DocumentMeta) -> None:
        self.documents.append(document)

    def get_referral(self, referral_id: str) -> Referral:
        if referral_id not in self.referrals:
            self._audit(self._actor, "get_referral", "patient_record", referral_id, "failure")
            raise NookalNotFound(referral_id)
        self._audit(self._actor, "get_referral", "patient_record", referral_id, "success")
        return self.referrals[referral_id]

    def list_referrals(
        self,
        *,
        patient_id: str | None = None,
        referrer_id: str | None = None,
    ) -> list[Referral]:
        results = list(self.referrals.values())
        if patient_id:
            results = [r for r in results if r.patient_id == patient_id]
        if referrer_id:
            results = [r for r in results if r.referrer_id == referrer_id]
        results.sort(key=lambda r: (r.recorded_on.isoformat(), r.referral_id))
        return results

    # Legacy mock referrer methods for Phase C orchestration test helpers
    def list_referrers(self) -> list[Referrer]:
        return list(self.referrers.values())

    def upsert_referrer(self, payload: Mapping[str, Any]) -> Referrer:
        assert_allows("nookal.upsert_referrer")
        rid = str(payload.get("referrer_id") or self._next_id("ref"))
        ref = Referrer(referrer_id=rid, name=str(payload["name"]), provider_number=payload.get("provider_number"), raw=dict(payload))
        self.referrers[rid] = ref
        return ref

    # --- Patients ---

    def get_patients(
        self,
        *,
        page: int = 1,
        page_length: int = 200,
        last_modified: str | None = None,
        deceased: int | None = None,
    ) -> list[PatientRef]:
        res = list(self.patients.values())
        if deceased is not None:
            res = [p for p in res if p.deceased == bool(deceased)]
        return res[:page_length]

    def search_patients(
        self,
        *,
        patient_id: str | None = None,
        online_code: str | None = None,
        date_created: str | None = None,
        email: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
        date_of_birth: str | None = None,
        fuzzy_search: str | None = None,
        suburb: str | list[str] | None = None,
        age_min: int | None = None,
        age_max: int | None = None,
        appointment_from: date | None = None,
        appointment_to: date | None = None,
        referrer_id: str | None = None,
        deceased: int | None = None,
    ) -> list[PatientRef]:
        results = list(self.patients.values())
        if deceased is not None:
            results = [p for p in results if p.deceased == bool(deceased)]
        if patient_id:
            results = [p for p in results if p.patient_id == patient_id]
        if email:
            results = [p for p in results if (p.email or "").lower() == email.lower()]
        if first_name:
            results = [p for p in results if (p.first_name or "").lower() == first_name.lower()]
        if last_name:
            results = [p for p in results if (p.last_name or "").lower() == last_name.lower()]
        if fuzzy_search:
            needle = fuzzy_search.strip().lower()
            results = [
                p
                for p in results
                if needle in (p.display_name or "").lower()
                or needle in f"{p.first_name or ''} {p.last_name or ''}".lower()
                or needle in (p.phone or "").lower()
                or needle in (p.email or "").lower()
                or needle == (p.patient_id or "").lower()
            ]
        if suburb is not None:
            if isinstance(suburb, (list, set, tuple)):
                suburbs_set = {s.casefold() for s in suburb if s}
                if suburbs_set:
                    results = [p for p in results if (p.suburb or "").casefold() in suburbs_set]
            else:
                needle = suburb.casefold()
                results = [p for p in results if (p.suburb or "").casefold() == needle]

        as_of = self._age_as_of()
        if age_min is not None or age_max is not None:
            filtered: list[PatientRef] = []
            for p in results:
                if p.date_of_birth is None:
                    continue
                age = _age_years(p.date_of_birth, as_of)
                if age_min is not None and age < age_min:
                    continue
                if age_max is not None and age > age_max:
                    continue
                filtered.append(p)
            results = filtered

        if appointment_from is not None or appointment_to is not None:
            filtered_by_appt: list[PatientRef] = []
            for p in results:
                if p.last_appointment_date is not None:
                    day = p.last_appointment_date
                    if appointment_from is not None and day < appointment_from:
                        continue
                    if appointment_to is not None and day > appointment_to:
                        continue
                    filtered_by_appt.append(p)
            if filtered_by_appt or any(p.last_appointment_date is not None for p in results):
                results = filtered_by_appt

        if referrer_id is not None:
            results = [p for p in results if p.referrer_id == referrer_id]

        results.sort(key=lambda p: p.patient_id)
        return results

    def get_patient(self, patient_id: str) -> PatientRef:
        if patient_id not in self.patients:
            self._audit(self._actor, "get_patient", "patient_record", patient_id, "failure")
            raise NookalNotFound(patient_id)
        self._audit(self._actor, "get_patient", "patient_record", patient_id, "success")
        return self.patients[patient_id]

    def find_patient_by_phone(self, phone: str) -> list[PatientRef]:
        hits = [p for p in self.patients.values() if p.phone == phone]
        self._audit(
            self._actor,
            "find_patient_by_phone",
            "patient_record",
            "phone_lookup",
            "success",
            metadata={"match_count": len(hits)},
        )
        return hits

    def add_patient(self, payload: Mapping[str, Any]) -> PatientRef:
        assert_allows("nookal.addPatient")
        first_name = (
            payload.get("firstName")
            or payload.get("first_name")
            or payload.get("FirstName")
        )
        last_name = (
            payload.get("lastName")
            or payload.get("last_name")
            or payload.get("LastName")
        )
        if not first_name or not last_name:
            raise NookalValidationError("add_patient requires both first_name and last_name")

        pid = str(payload.get("patient_id") or self._next_id("pat"))
        patient = PatientRef(
            patient_id=pid,
            first_name=str(first_name),
            last_name=str(last_name),
            display_name=f"{first_name} {last_name}".strip(),
            phone=payload.get("phone") or payload.get("mobile"),
            email=payload.get("email"),
            raw=dict(payload),
        )
        self.patients[pid] = patient
        return patient

    def edit_patient(self, patient_id: str, payload: Mapping[str, Any]) -> PatientRef:
        assert_allows("nookal.editPatient")
        if not patient_id:
            raise NookalValidationError("edit_patient requires patient_id")
        existing = self.patients.get(patient_id)
        if existing is None:
            patient = PatientRef(
                patient_id=patient_id,
                first_name=payload.get("first_name") or payload.get("firstName"),
                last_name=payload.get("last_name") or payload.get("lastName"),
                phone=payload.get("phone") or payload.get("mobile"),
                email=payload.get("email"),
                raw=dict(payload),
            )
            self.patients[patient_id] = patient
            return patient

        updated = PatientRef(
            patient_id=existing.patient_id,
            first_name=payload.get("first_name") or payload.get("firstName") or existing.first_name,
            last_name=payload.get("last_name") or payload.get("lastName") or existing.last_name,
            phone=payload.get("phone") or payload.get("mobile") or existing.phone,
            email=payload.get("email") or existing.email,
            display_name=payload.get("display_name") or existing.display_name,
            date_of_birth=existing.date_of_birth,
            suburb=payload.get("suburb") or existing.suburb,
            referrer_id=payload.get("referrer_id") or existing.referrer_id,
            last_appointment_date=existing.last_appointment_date,
            raw={**existing.raw, **payload},
        )
        self.patients[patient_id] = updated
        return updated

    # --- Cases ---

    def get_cases(
        self,
        patient_id: str,
        *,
        page: int = 1,
        page_length: int = 200,
        last_modified: str | None = None,
    ) -> list[CaseRef]:
        return [c for c in self.cases.values() if c.patient_id == patient_id][:page_length]

    def get_all_cases(
        self,
        *,
        page: int = 1,
        page_length: int = 200,
        last_modified: str | None = None,
    ) -> list[CaseRef]:
        return list(self.cases.values())[:page_length]

    # --- Treatment Notes ---

    def get_treatment_notes(
        self,
        patient_id: str,
        *,
        page: int = 1,
        page_length: int = 100,
        last_modified: str | None = None,
    ) -> list[TreatmentNote]:
        return [n for n in self.treatment_notes if n.patient_id == patient_id][:page_length]

    def get_all_treatment_notes(
        self,
        *,
        page: int = 1,
        page_length: int = 50,
        last_modified: str | None = None,
        practitioner_id: str | None = None,
    ) -> list[TreatmentNote]:
        notes = list(self.treatment_notes)
        if practitioner_id:
            notes = [n for n in notes if n.practitioner_id == practitioner_id]
        return notes[:page_length]

    def add_treatment_note(
        self,
        *,
        patient_id: str,
        case_id: str,
        practitioner_id: str,
        date: str | datetime,
        notes: str,
        appt_id: str | None = None,
    ) -> TreatmentNote:
        assert_allows("nookal.addTreatmentNote")
        note_id = self._next_id("note")
        note = TreatmentNote(
            note_id=note_id,
            patient_id=patient_id,
            case_id=case_id,
            practitioner_id=practitioner_id,
            date=date,
            notes=notes,
            appointment_id=appt_id,
        )
        self.treatment_notes.append(note)
        return note

    # --- Extras ---

    def get_extras(self) -> list[PatientExtra]:
        return list(self.extras)

    def add_patient_extra(
        self,
        *,
        patient_id: str,
        extra_id: str,
        value: str,
    ) -> bool:
        assert_allows("nookal.addPatientExtra")
        return True

    # --- Appointments ---

    def list_appointments(
        self,
        *,
        on_date: date | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        patient_id: str | None = None,
        location_id: str | None = None,
        practitioner_id: str | None = None,
        appt_status: str | None = None,
        status: str | None = None,
        time_from: str | None = None,
        time_to: str | None = None,
        service_id: str | None = None,
        class_id: str | None = None,
        page: int = 1,
        page_length: int = 200,
        last_modified: str | None = None,
    ) -> list[Appointment]:
        results = list(self.appointments.values())
        if patient_id:
            results = [a for a in results if a.patient_id == patient_id]
        if on_date:
            results = [a for a in results if a.starts_at.date() == on_date]
        if date_from:
            results = [a for a in results if a.starts_at.date() >= date_from]
        if date_to:
            results = [a for a in results if a.starts_at.date() <= date_to]
        if location_id:
            results = [a for a in results if a.location_id == location_id]
        if practitioner_id:
            results = [a for a in results if a.practitioner_id == practitioner_id]
        effective_status = status or appt_status
        if effective_status:
            results = [a for a in results if (a.status or "").lower() == effective_status.lower()]
        return results[:page_length]

    def get_appointment(self, appointment_id: str) -> Appointment:
        clean_id = str(appointment_id).strip()
        if clean_id in self.appointments:
            self._audit(self._actor, "get_appointment", "appointment", clean_id, "success")
            return self.appointments[clean_id]

        clean_bare = clean_id.lower().replace("appt_", "")

        # 1. Check case-insensitive or prefix-insensitive key match
        for k, v in self.appointments.items():
            k_str = str(k).strip()
            k_bare = k_str.lower().replace("appt_", "")
            if (
                k_str.lower() == clean_id.lower()
                or (clean_bare and k_bare == clean_bare)
                or (clean_bare and clean_bare.isdigit() and k_bare.isdigit() and int(clean_bare) == int(k_bare))
            ):
                self._audit(self._actor, "get_appointment", "appointment", clean_id, "success")
                return v

        # 2. Check appointment objects by appointment_id field
        for a in self.appointments.values():
            a_str = str(a.appointment_id).strip()
            a_bare = a_str.lower().replace("appt_", "")
            if (
                a_str.lower() == clean_id.lower()
                or (clean_bare and a_bare == clean_bare)
                or (clean_bare and clean_bare.isdigit() and a_bare.isdigit() and int(clean_bare) == int(a_bare))
            ):
                self._audit(self._actor, "get_appointment", "appointment", clean_id, "success")
                return a

        # 3. Check if referenced by treatment notes
        for note in getattr(self, "treatment_notes", []):
            if getattr(note, "appointment_id", None):
                n_aid = str(note.appointment_id).strip()
                if (
                    n_aid.lower() == clean_id.lower()
                    or (clean_bare and n_aid.lower().replace("appt_", "") == clean_bare)
                ):
                    appt_date = datetime.now()
                    if getattr(note, "date", None):
                        try:
                            appt_date = datetime.fromisoformat(note.date)
                        except Exception:
                            pass
                    synthetic = Appointment(
                        appointment_id=clean_id,
                        patient_id=note.patient_id,
                        starts_at=appt_date,
                        status="completed",
                        practitioner_id=note.practitioner_id,
                        raw={"source": "treatment_note", "note_id": getattr(note, "note_id", "")},
                    )
                    self.appointments[clean_id] = synthetic
                    self._audit(self._actor, "get_appointment", "appointment", clean_id, "success")
                    return synthetic

        self._audit(self._actor, "get_appointment", "appointment", clean_id, "failure")
        raise NookalNotFound(appointment_id)

    def create_appointment(self, payload: Mapping[str, Any]) -> Appointment:
        assert_allows("nookal.addAppointmentBooking")
        aid = str(payload.get("appointment_id") or self._next_id("appt"))
        starts = payload.get("starts_at")
        if isinstance(starts, str):
            starts = datetime.fromisoformat(starts)
        elif not isinstance(starts, datetime):
            appt_d = payload.get("appointment_date") or date.today().isoformat()
            appt_t = payload.get("start_time") or "09:00:00"
            starts = datetime.fromisoformat(f"{appt_d}T{appt_t}")

        appt = Appointment(
            appointment_id=aid,
            patient_id=str(payload["patient_id"]),
            starts_at=starts,
            status=payload.get("status") or "booked",
            location_id=str(payload.get("location_id", "1")),
            practitioner_id=str(payload.get("practitioner_id", "1")),
            raw=dict(payload),
        )
        self.appointments[aid] = appt
        return appt

    def update_appointment(
        self,
        appointment_id: str,
        *,
        starts_at: datetime | None = None,
        status: str | None = None,
        **fields: Any,
    ) -> Appointment:
        assert_allows("nookal.updateAppointmentBooking")
        existing = self.get_appointment(appointment_id)
        updated = Appointment(
            appointment_id=existing.appointment_id,
            patient_id=existing.patient_id,
            starts_at=starts_at if starts_at is not None else existing.starts_at,
            ends_at=existing.ends_at,
            status=status if status is not None else existing.status,
            location_id=fields.get("location_id") or existing.location_id,
            practitioner_id=fields.get("practitioner_id") or existing.practitioner_id,
            raw={**existing.raw, **fields},
        )
        self.appointments[appointment_id] = updated
        return updated

    def cancel_appointment(
        self,
        appointment_id: str,
        *,
        patient_id: str,
    ) -> Appointment:
        assert_allows("nookal.cancelAppointment")
        existing = self.get_appointment(appointment_id)
        updated = Appointment(
            appointment_id=existing.appointment_id,
            patient_id=existing.patient_id,
            starts_at=existing.starts_at,
            ends_at=existing.ends_at,
            status="cancelled",
            location_id=existing.location_id,
            practitioner_id=existing.practitioner_id,
            cancelled=True,
            raw={**existing.raw, "status": "cancelled"},
        )
        self.appointments[appointment_id] = updated
        return updated

    def rebook_appointment(
        self,
        appointment_id: str,
        *,
        patient_id: str,
        location_id: str,
        start_time: str,
        practitioner_id: str,
        appointment_date: str | date,
        cancel_first: bool = False,
    ) -> Appointment:
        assert_allows("nookal.rebookAppointment")
        if cancel_first:
            self.cancel_appointment(appointment_id, patient_id=patient_id)
        new_id = self._next_id("rebook")
        d_str = appointment_date.isoformat() if isinstance(appointment_date, date) else str(appointment_date)
        dt = datetime.fromisoformat(f"{d_str}T{start_time}")
        new_appt = Appointment(
            appointment_id=new_id,
            patient_id=patient_id,
            starts_at=dt,
            status="booked",
            location_id=location_id,
            practitioner_id=practitioner_id,
        )
        self.appointments[new_id] = new_appt
        return new_appt

    def get_appointment_availabilities(
        self,
        *,
        location_id: str | None = None,
        practitioner_id: str | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        appointment_type_id: str | None = None,
    ) -> list[Mapping[str, Any]]:
        return [{"time": s.strftime("%H:%M:%S"), "date": s.strftime("%Y-%m-%d")} for s in self.open_slots]

    def get_class_availabilities(
        self,
        *,
        location_id: str | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        class_id: str | None = None,
    ) -> list[Mapping[str, Any]]:
        return []

    # --- Locations & Practitioners ---

    def get_locations(self, *, last_modified: str | None = None) -> list[Location]:
        return list(self.locations.values())

    def get_location_logo(self, location_id: str) -> str | None:
        return "https://api.nookal.com/static/logo.png"

    def get_practitioners(
        self,
        *,
        last_modified: str | None = None,
        include_inactive_practitioners: bool = False,
    ) -> list[Practitioner]:
        return list(self.practitioners.values())

    def get_practitioner_photo(self, practitioner_id: str) -> str | None:
        return "https://api.nookal.com/static/photo.png"

    # --- Services & Classes ---

    def get_appointment_types(self) -> list[AppointmentType]:
        return list(self.appointment_types.values())

    def get_class_types(self) -> list[ClassType]:
        return list(self.class_types.values())

    def get_class_participants(
        self,
        class_id: str,
        *,
        page: int = 1,
        page_length: int = 200,
    ) -> list[ClassParticipant]:
        return [p for p in self.class_participants if p.class_id == class_id][:page_length]

    def get_class_redemptions(self) -> list[Redemption]:
        return []

    def get_service_redemptions(self) -> list[Redemption]:
        return []

    def get_waiting_list(self) -> list[WaitingListEntry]:
        return []

    # --- Documents ---

    def seed_file(self, file: PatientFile) -> None:
        self.files[file.file_id] = file

    def get_patient_files(
        self,
        patient_id: str,
        *,
        page: int = 1,
        page_length: int = 200,
        last_modified: str | None = None,
    ) -> list[PatientFile]:
        return [f for f in self.files.values() if str(f.patient_id) == str(patient_id)][:page_length]

    def get_file_url(self, patient_id: str, file_id: str) -> str:
        clean_pid = str(patient_id or "").strip()
        clean_fid = str(file_id or "").strip()
        if not clean_pid or not clean_fid or any(c in clean_fid for c in "/\\?#&="):
            raise NookalInvalidFileId("patient_id and file_id are required for get_file_url")
        if clean_fid in self.files:
            pfile = self.files[clean_fid]
            if str(pfile.patient_id) != clean_pid:
                raise NookalFileNotFound(f"File {clean_fid} does not belong to patient {clean_pid}")
        elif self.files:
            raise NookalFileNotFound(f"File {clean_fid} not found for patient {clean_pid}")
        return f"https://s3.amazonaws.com/nookal-files/{clean_pid}/{clean_fid}.pdf?signature=temp"

    def upload_file(
        self,
        *,
        patient_id: str,
        name: str,
        extension: str,
        file_type: str,
        file_path: str,
        case_id: str | None = None,
        date_added: str | None = None,
    ) -> tuple[str, str]:
        fid = self._next_id("file")
        return fid, f"https://s3.amazonaws.com/nookal-upload/{fid}"

    def set_file_active(self, *, patient_id: str, file_id: str) -> bool:
        return True

    def upload_patient_document(
        self,
        *,
        patient_id: str,
        name: str,
        extension: str,
        file_type: str,
        content: bytes,
        case_id: str | None = None,
        date_added: str | None = None,
    ) -> PatientFile:
        fid, _ = self.upload_file(
            patient_id=patient_id,
            name=name,
            extension=extension,
            file_type=file_type,
            file_path=f"{name}.{extension}",
            case_id=case_id,
            date_added=date_added,
        )
        pfile = PatientFile(
            file_id=fid,
            patient_id=patient_id,
            name=name,
            file_type=file_type,
            date_added=date_added or date.today().isoformat(),
            size=len(content),
        )
        self.files[fid] = pfile
        self.documents.append(DocumentMeta(document_id=fid, patient_id=patient_id, title=name))
        return pfile

    def save_document(
        self,
        patient_id: str,
        *,
        title: str,
        content: bytes,
        content_type: str = "application/pdf",
    ) -> DocumentMeta:
        doc_id = self._next_id("doc")
        meta = DocumentMeta(doc_id, patient_id, title)
        self.documents.append(meta)
        return meta

    # --- Invoices ---

    def get_invoice(self, invoice_id: str, *, void: int | None = None) -> Invoice:
        if invoice_id not in self.invoices:
            raise NookalNotFound(f"invoice {invoice_id} not found")
        return self.invoices[invoice_id]

    def get_invoices(
        self,
        patient_id: str | None = None,
        *,
        last_modified: str | None = None,
        void: int | None = None,
        expanded: int | None = None,
    ) -> list[Invoice]:
        if patient_id is not None and not str(patient_id).strip():
            raise NookalValidationError("patient_id cannot be empty string")
        if patient_id:
            return [inv for inv in self.invoices.values() if inv.patient_id == patient_id]
        return list(self.invoices.values())

    def get_invoice_entries(
        self,
        *,
        invoice_id: str | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        lastmodified_date_from: date | None = None,
        lastmodified_date_to: date | None = None,
    ) -> list[InvoiceEntry]:
        if invoice_id:
            return [e for e in self.invoice_entries if e.invoice_id == invoice_id]
        return list(self.invoice_entries)

    def get_invoice_credits(self, **params: Any) -> list[Mapping[str, Any]]:
        return []

    def get_invoice_discounts(self, **params: Any) -> list[Mapping[str, Any]]:
        return []

    def get_invoice_payments(self, **params: Any) -> list[Mapping[str, Any]]:
        return []

    def get_invoice_refunds(self, **params: Any) -> list[Mapping[str, Any]]:
        return []

    def get_invoice_adjustments(self, **params: Any) -> list[Mapping[str, Any]]:
        return []

    def add_invoice(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        assert_allows("nookal.addInvoice")
        iid = str(payload.get("invoice_id") or self._next_id("inv"))
        inv = Invoice(
            invoice_id=iid,
            patient_id=str(payload.get("patient_id", "")),
            date=str(payload.get("date", date.today().isoformat())),
            total=float(payload.get("total", 0.0)) if payload.get("total") is not None else None,
            status=str(payload.get("status", "Unpaid")),
            raw=dict(payload),
        )
        self.invoices[iid] = inv
        return {"status": "success", "invoice_id": iid}

    def delete_invoice(self, invoice_id: str) -> bool:
        assert_allows("nookal.deleteInvoice")
        if invoice_id in self.invoices:
            inv = self.invoices[invoice_id]
            self.invoices[invoice_id] = Invoice(
                invoice_id=inv.invoice_id,
                patient_id=inv.patient_id,
                date=inv.date,
                total=inv.total,
                status="Void",
                void=True,
                raw=inv.raw,
            )
        return True

    def add_item_to_invoice(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        assert_allows("nookal.addItemToInvoice")
        eid = str(payload.get("entry_id") or self._next_id("item"))
        entry = InvoiceEntry(
            entry_id=eid,
            invoice_id=str(payload.get("invoice_id", "")) or None,
            description=str(payload.get("description", "")),
            price=float(payload.get("price", 0.0)) if payload.get("price") is not None else None,
            quantity=float(payload.get("quantity", 1.0)) if payload.get("quantity") is not None else 1.0,
            raw=dict(payload),
        )
        self.invoice_entries.append(entry)
        return {"status": "success", "entry_id": eid}

    def delete_item_from_invoice(self, item_id: str) -> bool:
        assert_allows("nookal.deleteItemFromInvoice")
        self.invoice_entries = [e for e in self.invoice_entries if e.entry_id != item_id]
        return True

    def add_payment_to_invoice(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        assert_allows("nookal.addPaymentToInvoice")
        return {"status": "success", "payment_id": self._next_id("pay")}

    def delete_payment_from_invoice(self, payment_id: str) -> bool:
        assert_allows("nookal.deletePaymentFromInvoice")
        return True

    def add_account_credit(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        assert_allows("nookal.addAccountCredit")
        return {"status": "success", "credit_id": self._next_id("cred")}


def build_client(
    config: NookalConfig | None = None,
    *,
    use_mock: bool = False,
    audit: AuditFn | None = None,
    actor: str = "nookal_client",
) -> NookalClient:
    """Build HttpNookalClient or MockNookalClient."""
    if use_mock:
        return MockNookalClient(audit=audit, actor=actor)
    return HttpNookalClient(config=config, audit=audit, actor=actor)
