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

import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Callable, Mapping
import httpx

from app.shared import audit as audit_mod
from app.shared.config import NookalConfig, get_settings
from app.shared.exceptions import (
    KillSwitchActive,
    NookalAuthError,
    NookalError,
    NookalNotFound,
    NookalRateLimit,
    NookalServerError,
    NookalValidationError,
)
from app.shared.kill_switch import assert_allows


AuditFn = Callable[..., Any]

_SECRET_RE = re.compile(r"([?&]api_key=)[^&\s'\"]+", re.IGNORECASE)


def _redact_secrets(text: str, api_key: str | None = None) -> str:
    """Scrub api_key values from query parameters and raw strings."""
    if not text:
        return text
    redacted = _SECRET_RE.sub(r"\1[REDACTED]", str(text))
    if api_key and api_key.strip():
        redacted = redacted.replace(api_key, "[REDACTED]")
    return redacted


def _unwrap_collection(data: Any, preferred_key: str | None = None) -> list[Any]:
    """
    Unwrap collection from Nookal data payloads.
    Handles lists, dicts of records, or wrapped dict keys (e.g. {'patients': [...]}).
    """
    if data is None:
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, Mapping):
        if preferred_key and preferred_key in data and isinstance(data[preferred_key], list):
            return data[preferred_key]
        for candidate in ("patients", "appointments", "results", "items", "data", "availabilities"):
            if candidate in data and isinstance(data[candidate], list):
                return data[candidate]
        if data and all(isinstance(v, Mapping) for v in data.values()):
            return list(data.values())
        if not data:
            return []
    return []


@dataclass(frozen=True)
class PatientRef:
    """
    Minimal patient handle for automation.

    Optional demographic fields support mock/dashboard filtering only —
    not clinical content. Existing call sites that pass only patient_id
    (+ phone/email/display_name) remain valid.
    """

    patient_id: str
    phone: str | None = None
    email: str | None = None
    display_name: str | None = None  # staff UI only; never put in audit metadata
    date_of_birth: date | None = None
    suburb: str | None = None
    referrer_id: str | None = None
    last_appointment_date: date | None = None


@dataclass(frozen=True)
class Appointment:
    appointment_id: str
    patient_id: str
    starts_at: datetime
    ends_at: datetime | None = None
    status: str | None = None
    location_id: str | None = None
    practitioner_id: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)


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
    """
    Clinic-side synthetic referral for mock/tests only.

    This is NOT a claim about Nookal's real referral resource schema.
    Live HttpNookalClient does not implement referral endpoints until
    official docs are confirmed.
    """

    referral_id: str
    patient_id: str
    referrer_id: str
    recorded_on: date
    notes_ref: str | None = None  # opaque id/ref only — never clinical free text here


class NookalClient(ABC):
    """Shared interface for live + mock clients."""

    # --- reads ---

    @abstractmethod
    def get_patient(self, patient_id: str) -> PatientRef:
        ...

    @abstractmethod
    def find_patient_by_phone(self, phone: str) -> list[PatientRef]:
        ...

    @abstractmethod
    def list_appointments(
        self,
        *,
        on_date: date | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        patient_id: str | None = None,
    ) -> list[Appointment]:
        ...

    @abstractmethod
    def get_appointment(self, appointment_id: str) -> Appointment:
        ...

    @abstractmethod
    def search_patients(
        self,
        *,
        suburb: str | None = None,
        age_min: int | None = None,
        age_max: int | None = None,
        appointment_from: date | None = None,
        appointment_to: date | None = None,
        referrer_id: str | None = None,
    ) -> list[PatientRef]:
        ...

    @abstractmethod
    def list_referrers(self) -> list[Referrer]:
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

    # --- writes (kill-switch checked; patient-facing flows still go via approval) ---

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
    def create_appointment(self, payload: Mapping[str, Any]) -> Appointment:
        ...

    @abstractmethod
    def cancel_appointment(
        self,
        appointment_id: str,
        *,
        patient_id: str | None = None,
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
    def save_document(
        self,
        patient_id: str,
        *,
        title: str,
        content: bytes,
        content_type: str = "application/pdf",
    ) -> DocumentMeta:
        ...

    @abstractmethod
    def upsert_referrer(self, payload: Mapping[str, Any]) -> Referrer:
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
        """
        Headers for Nookal API requests.
        Authentication is via query parameter (?api_key=), NOT Authorization header.
        """
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

        # Centralized query parameter authentication
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
                    # Do not blindly retry non-idempotent writes
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
            raise NookalNotFound(f"{action}: {target_id} not found")
        if status == 422:
            raise NookalValidationError(f"validation error on {action}")
        if status == 429:
            retry_after = response.headers.get("Retry-After")
            exc = NookalRateLimit("rate limited by Nookal")
            exc.retry_after = float(retry_after) if retry_after and retry_after.isdigit() else None
            raise exc
        if status >= 500:
            redacted = _redact_secrets(response.text[:200], self._config.api_key)
            raise NookalServerError(f"Nookal {status} on {action}: {redacted}")
        if status >= 400:
            raise NookalError(f"Nookal {status} on {action}")

        if status == 204 or not response.content:
            return None

        try:
            payload = response.json()
        except ValueError as exc:
            raise NookalError(f"non-JSON response on {action}") from exc

        # Handle Nookal envelope: {"status": "...", "data": ..., "details": ...}
        if isinstance(payload, Mapping):
            nookal_status = str(payload.get("status", "")).lower()
            if nookal_status in ("failure", "error"):
                details = (
                    payload.get("details")
                    or payload.get("message")
                    or payload.get("error")
                    or "API error"
                )
                sanitized_details = _redact_secrets(str(details), self._config.api_key)
                lower_details = sanitized_details.lower()
                if any(x in lower_details for x in ("not found", "no records found", "0 results", "does not exist")):
                    raise NookalNotFound(f"{action}: {sanitized_details}")
                if any(x in lower_details for x in ("auth", "unauthorized", "api key", "invalid key", "forbidden")):
                    raise NookalAuthError(f"auth failed on {action}: {sanitized_details}")
                raise NookalError(f"Nookal error on {action}: {sanitized_details}")

            if "data" in payload:
                return payload["data"]

        return payload

    # --- reads ---

    def get_patient(self, patient_id: str) -> PatientRef:
        """
        Fetch patient by patient_id using official /searchPatients endpoint.
        """
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
                pid = r.get("ID") or r.get("id") or r.get("patient_id") or r.get("PatientID")
                if pid:
                    matched_ids.add(str(pid))

        if len(matched_ids) > 1:
            raise NookalValidationError(f"ambiguous patient matches for {patient_id}")

        matching = next(
            (
                r
                for r in rows
                if isinstance(r, Mapping)
                and str(r.get("ID") or r.get("id") or r.get("patient_id") or r.get("PatientID"))
                == str(patient_id)
            ),
            rows[0],
        )
        return self._parse_patient(matching)

    def find_patient_by_phone(self, phone: str) -> list[PatientRef]:
        """
        Lookup patient by phone using official /searchPatients?fuzzy_search=.
        Filters client-side for exact phone match. Audit target is 'phone_lookup' (no PII).
        """
        data = self._request(
            "GET",
            "/searchPatients",
            action="find_patient_by_phone",
            target_type="patient_record",
            target_id="phone_lookup",
            params={"fuzzy_search": phone},
        )
        rows = _unwrap_collection(data, "patients")
        patients = [self._parse_patient(row) for row in rows if isinstance(row, Mapping)]
        clean_phone = re.sub(r"[^\d+]", "", phone)
        matched = []
        for p in patients:
            if p.phone:
                p_clean = re.sub(r"[^\d+]", "", p.phone)
                if p.phone == phone or (clean_phone and p_clean == clean_phone):
                    matched.append(p)
        return matched

    def list_appointments(
        self,
        *,
        on_date: date | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        patient_id: str | None = None,
    ) -> list[Appointment]:
        """
        Retrieve appointments using official /getAppointments endpoint.
        Maps on_date to matching date_from and date_to parameters.
        """
        params: dict[str, Any] = {
            "page": 1,
            "page_length": 200,
        }
        if on_date:
            params["date_from"] = on_date.isoformat()
            params["date_to"] = on_date.isoformat()
        else:
            if date_from:
                params["date_from"] = date_from.isoformat()
            if date_to:
                params["date_to"] = date_to.isoformat()
        if patient_id:
            params["patient_id"] = patient_id

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
        """
        Retrieve single appointment by ID using official /getAppointments endpoint.
        """
        params: dict[str, Any] = {
            "page_length": 200,
        }
        data = self._request(
            "GET",
            "/getAppointments",
            action="get_appointment",
            target_type="appointment",
            target_id=appointment_id,
            params=params,
        )
        rows = _unwrap_collection(data, "appointments")
        for row in rows:
            if isinstance(row, Mapping):
                aid = (
                    row.get("ID")
                    or row.get("id")
                    or row.get("appointment_id")
                    or row.get("appointmentID")
                )
                if aid and str(aid) == str(appointment_id):
                    return self._parse_appointment(row)
        raise NookalNotFound(f"get_appointment: appointment {appointment_id} not found")

    def search_patients(
        self,
        *,
        suburb: str | None = None,
        age_min: int | None = None,
        age_max: int | None = None,
        appointment_from: date | None = None,
        appointment_to: date | None = None,
        referrer_id: str | None = None,
    ) -> list[PatientRef]:
        """
        Search patients using official /getPatients endpoint and client-side demographic filters.
        """
        params: dict[str, Any] = {
            "page": 1,
            "page_length": 200,
        }
        data = self._request(
            "GET",
            "/getPatients",
            action="search_patients",
            target_type="patient_record",
            target_id="search",
            params=params,
        )
        rows = _unwrap_collection(data, "patients")
        results = [self._parse_patient(row) for row in rows if isinstance(row, Mapping)]

        if suburb is not None:
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

    def get_appointment_availabilities(
        self,
        *,
        location_id: str | None = None,
        practitioner_id: str | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        appointment_type_id: str | None = None,
    ) -> list[Mapping[str, Any]]:
        """
        Fetch practitioner availabilities using official /getAppointmentAvailabilities endpoint.
        """
        params: dict[str, Any] = {}
        if location_id:
            params["location_id"] = location_id
        if practitioner_id:
            params["practitioner_id"] = practitioner_id
        if date_from:
            params["date_from"] = date_from.isoformat()
        if date_to:
            params["date_to"] = date_to.isoformat()
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

    def list_referrers(self) -> list[Referrer]:
        """
        Unsupported on live client: Nookal API v2 does not expose a public referrer endpoint.
        """
        raise NotImplementedError(
            "list_referrers: Nookal API v2 does not expose a public referrer endpoint; see documentation"
        )

    # --- writes ---

    def create_appointment(self, payload: Mapping[str, Any]) -> Appointment:
        """
        Create appointment booking using official /addAppointmentBooking endpoint.
        Validates required fields before sending request.
        """
        body: dict[str, Any] = {}

        starts = payload.get("starts_at")
        if starts:
            if isinstance(starts, str):
                try:
                    starts = datetime.fromisoformat(starts)
                except ValueError:
                    pass
            if isinstance(starts, datetime):
                body.setdefault("appointment_date", starts.date().isoformat())
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
        """
        Update appointment booking using official /updateAppointmentBooking endpoint.
        Uses allowlist of supported Nookal fields.
        """
        body: dict[str, Any] = {"appointment_id": appointment_id}
        if starts_at is not None:
            body["appointment_date"] = starts_at.date().isoformat()
            body["start_time"] = starts_at.time().strftime("%H:%M:%S")
        if status is not None:
            body["status"] = status
            if status == "cancelled":
                body["cancelled"] = "1"
            elif status == "dna":
                body["dna"] = "1"
            elif status == "arrived":
                body["arrived"] = "1"

        allowed_fields = {
            "appointment_date",
            "start_time",
            "end_time",
            "location_id",
            "practitioner_id",
            "appointment_type_id",
            "notes",
            "arrived",
            "dna",
            "cancelled",
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
        patient_id: str | None = None,
    ) -> Appointment:
        """
        Cancel appointment using official /cancelAppointment endpoint.
        """
        body: dict[str, Any] = {"appointment_id": appointment_id}
        if patient_id:
            body["patient_id"] = patient_id

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
            patient_id=str(patient_id or ""),
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
        """
        Rebook appointment using official /rebookAppointment endpoint.
        """
        date_str = (
            appointment_date.isoformat()
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

    def save_document(
        self,
        patient_id: str,
        *,
        title: str,
        content: bytes,
        content_type: str = "application/pdf",
    ) -> DocumentMeta:
        """
        Unsupported on live client: official Nookal v2 document upload requires a two-step
        presigned S3 workflow (/uploadFile -> direct S3 upload -> /setFileActive).
        """
        raise NotImplementedError(
            "save_document: official Nookal v2 document upload requires a two-step presigned "
            "S3 workflow (/uploadFile -> direct S3 upload -> /setFileActive); "
            "intentional NotImplementedError until full S3 upload pipeline is configured."
        )

    def upsert_referrer(self, payload: Mapping[str, Any]) -> Referrer:
        """
        Unsupported on live client: Nookal API v2 does not expose a public referrer endpoint.
        """
        raise NotImplementedError(
            "upsert_referrer: Nookal API v2 does not expose a public referrer endpoint; see documentation"
        )

    # --- parsers ---

    @staticmethod
    def _parse_patient(data: Any) -> PatientRef:
        if not isinstance(data, Mapping):
            raise NookalValidationError("unexpected patient payload shape")
        pid = (
            data.get("ID")
            or data.get("id")
            or data.get("patient_id")
            or data.get("PatientID")
        )
        if not pid:
            raise NookalValidationError("patient payload missing id")

        first = data.get("first_name") or data.get("FirstName") or data.get("firstname") or ""
        last = data.get("last_name") or data.get("LastName") or data.get("lastname") or ""
        full = (f"{first} {last}".strip()) or data.get("name") or data.get("full_name") or data.get("Name") or None

        phone = (
            data.get("mobile")
            or data.get("Mobile")
            or data.get("phone")
            or data.get("telephone")
            or data.get("Telephone")
        )
        email = data.get("email") or data.get("Email")

        dob_raw = data.get("DOB") or data.get("date_of_birth") or data.get("dob")
        parsed_dob = None
        if dob_raw:
            if isinstance(dob_raw, date):
                parsed_dob = dob_raw
            elif isinstance(dob_raw, str) and len(dob_raw) >= 10:
                try:
                    parsed_dob = date.fromisoformat(dob_raw[:10])
                except ValueError:
                    pass

        suburb = data.get("suburb") or data.get("Suburb") or data.get("address")
        ref_id = data.get("referrer_id") or data.get("referrerID")

        return PatientRef(
            patient_id=str(pid),
            phone=phone,
            email=email,
            display_name=full,
            date_of_birth=parsed_dob,
            suburb=str(suburb) if suburb else None,
            referrer_id=str(ref_id) if ref_id else None,
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
        )
        pid = (
            data.get("patientID")
            or data.get("patient_id")
            or data.get("PatientID")
        )
        if not aid or not pid:
            raise NookalValidationError("appointment payload missing required fields (id, patient_id)")

        appt_date_raw = data.get("date") or data.get("appointment_date")
        start_time_raw = data.get("startTime") or data.get("start_time")
        end_time_raw = data.get("endTime") or data.get("end_time")

        starts_at = None
        if appt_date_raw and start_time_raw:
            try:
                starts_at = datetime.fromisoformat(f"{appt_date_raw}T{start_time_raw}")
            except ValueError:
                pass

        if starts_at is None:
            starts = data.get("starts_at") or data.get("start") or data.get("datetime")
            if isinstance(starts, datetime):
                starts_at = starts
            elif starts:
                try:
                    starts_at = datetime.fromisoformat(str(starts).replace("Z", "+00:00"))
                except ValueError:
                    pass

        if starts_at is None:
            raise NookalValidationError("appointment payload missing start time/date")

        ends_at = None
        if appt_date_raw and end_time_raw:
            try:
                ends_at = datetime.fromisoformat(f"{appt_date_raw}T{end_time_raw}")
            except ValueError:
                pass

        if ends_at is None:
            ends = data.get("ends_at") or data.get("end")
            if isinstance(ends, datetime):
                ends_at = ends
            elif ends:
                try:
                    ends_at = datetime.fromisoformat(str(ends).replace("Z", "+00:00"))
                except ValueError:
                    pass

        if str(data.get("cancelled", "")).strip() in ("1", "true", "True") or data.get("cancellationDate"):
            status = "cancelled"
        elif str(data.get("DNA", "")).strip() in ("1", "true", "True"):
            status = "dna"
        elif str(data.get("arrived", "")).strip() in ("1", "true", "True"):
            status = "arrived"
        elif data.get("status"):
            status = str(data["status"])
        else:
            status = "booked"

        loc_id = data.get("locationID") or data.get("location_id")
        prac_id = data.get("practitionerID") or data.get("practitioner_id")

        return Appointment(
            appointment_id=str(aid),
            patient_id=str(pid),
            starts_at=starts_at,
            ends_at=ends_at,
            status=status,
            location_id=str(loc_id) if loc_id else None,
            practitioner_id=str(prac_id) if prac_id else None,
            raw=dict(data),
        )


def _age_years(dob: date, as_of: date) -> int:
    years = as_of.year - dob.year
    if (as_of.month, as_of.day) < (dob.month, dob.day):
        years -= 1
    return years


class MockNookalClient(NookalClient):
    """In-memory stand-in for tests and dry-runs. Same interface as HttpNookalClient."""

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
        self.referrers: dict[str, Referrer] = {}
        self.referrals: dict[str, Referral] = {}
        self.documents: list[DocumentMeta] = []
        self.open_slots: list[datetime] = []
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
                    phone=patient.phone,
                    email=patient.email,
                    display_name=patient.display_name,
                    date_of_birth=patient.date_of_birth,
                    suburb=patient.suburb,
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
            self._audit(
                self._actor, "get_referral", "patient_record", referral_id, "failure"
            )
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
        self._audit(
            self._actor,
            "list_referrals",
            "patient_record",
            patient_id or referrer_id or "all",
            "success",
            metadata={"count": len(results)},
        )
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

    def list_appointments(
        self,
        *,
        on_date: date | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        patient_id: str | None = None,
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
        self._audit(
            self._actor,
            "list_appointments",
            "appointment",
            patient_id or "range",
            "success",
            metadata={"count": len(results)},
        )
        return results

    def get_appointment(self, appointment_id: str) -> Appointment:
        if appointment_id not in self.appointments:
            self._audit(self._actor, "get_appointment", "appointment", appointment_id, "failure")
            raise NookalNotFound(appointment_id)
        self._audit(self._actor, "get_appointment", "appointment", appointment_id, "success")
        return self.appointments[appointment_id]

    def search_patients(
        self,
        *,
        suburb: str | None = None,
        age_min: int | None = None,
        age_max: int | None = None,
        appointment_from: date | None = None,
        appointment_to: date | None = None,
        referrer_id: str | None = None,
    ) -> list[PatientRef]:
        results = list(self.patients.values())
        as_of = self._age_as_of()

        if suburb is not None:
            needle = suburb.casefold()
            results = [p for p in results if (p.suburb or "").casefold() == needle]

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
            matching_ids: set[str] = set()
            for appt in self.appointments.values():
                day = appt.starts_at.date()
                if appointment_from is not None and day < appointment_from:
                    continue
                if appointment_to is not None and day > appointment_to:
                    continue
                matching_ids.add(appt.patient_id)
            results = [p for p in results if p.patient_id in matching_ids]

        if referrer_id is not None:
            results = [p for p in results if p.referrer_id == referrer_id]

        results.sort(key=lambda p: p.patient_id)
        self._audit(
            self._actor,
            "search_patients",
            "patient_record",
            "search",
            "success",
            metadata={"count": len(results)},
        )
        return results

    def list_referrers(self) -> list[Referrer]:
        results = list(self.referrers.values())
        self._audit(self._actor, "list_referrers", "patient_record", "referrers", "success")
        return results

    def get_appointment_availabilities(
        self,
        *,
        location_id: str | None = None,
        practitioner_id: str | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        appointment_type_id: str | None = None,
    ) -> list[Mapping[str, Any]]:
        slots = self.open_slots
        if date_from:
            slots = [s for s in slots if s.date() >= date_from]
        if date_to:
            slots = [s for s in slots if s.date() <= date_to]
        self._audit(
            self._actor,
            "get_appointment_availabilities",
            "appointment",
            "availabilities",
            "success",
            metadata={"count": len(slots)},
        )
        return [{"start": s.isoformat()} for s in sorted(slots)]

    def update_appointment(
        self,
        appointment_id: str,
        *,
        starts_at: datetime | None = None,
        status: str | None = None,
        **fields: Any,
    ) -> Appointment:
        try:
            assert_allows("nookal.update_appointment")
        except KillSwitchActive:
            self._audit(
                self._actor,
                "update_appointment",
                "appointment",
                appointment_id,
                "blocked",
                metadata={"reason": "kill_switch"},
            )
            raise

        current = self.get_appointment(appointment_id)
        updated = Appointment(
            appointment_id=current.appointment_id,
            patient_id=current.patient_id,
            starts_at=starts_at or current.starts_at,
            ends_at=current.ends_at,
            status=status if status is not None else current.status,
            location_id=current.location_id,
            practitioner_id=current.practitioner_id,
            raw={**dict(current.raw), **fields},
        )
        self.appointments[appointment_id] = updated
        self._audit(self._actor, "update_appointment", "appointment", appointment_id, "success")
        return updated

    def create_appointment(self, payload: Mapping[str, Any]) -> Appointment:
        try:
            assert_allows("nookal.create_appointment")
        except KillSwitchActive:
            self._audit(
                self._actor,
                "create_appointment",
                "appointment",
                str(payload.get("patient_id", "new")),
                "blocked",
                metadata={"reason": "kill_switch"},
            )
            raise

        aid = self._next_id("appt")
        starts = payload.get("starts_at")
        if isinstance(starts, str):
            starts = datetime.fromisoformat(starts)
        if not isinstance(starts, datetime):
            raise NookalValidationError("starts_at required")
        appt = Appointment(
            appointment_id=aid,
            patient_id=str(payload["patient_id"]),
            starts_at=starts,
            status=str(payload.get("status", "booked")),
            raw=dict(payload),
        )
        self.appointments[aid] = appt
        self._audit(self._actor, "create_appointment", "appointment", aid, "success")
        return appt

    def cancel_appointment(
        self,
        appointment_id: str,
        *,
        patient_id: str | None = None,
    ) -> Appointment:
        try:
            assert_allows("nookal.cancel_appointment")
        except KillSwitchActive:
            self._audit(
                self._actor,
                "cancel_appointment",
                "appointment",
                appointment_id,
                "blocked",
                metadata={"reason": "kill_switch"},
            )
            raise

        return self.update_appointment(appointment_id, status="cancelled")

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
        try:
            assert_allows("nookal.rebook_appointment")
        except KillSwitchActive:
            self._audit(
                self._actor,
                "rebook_appointment",
                "appointment",
                appointment_id,
                "blocked",
                metadata={"reason": "kill_switch"},
            )
            raise

        date_str = (
            appointment_date.isoformat()
            if isinstance(appointment_date, date)
            else str(appointment_date)
        )
        dt = datetime.fromisoformat(f"{date_str}T{start_time}")
        if cancel_first:
            self.update_appointment(appointment_id, status="cancelled")

        new_aid = self._next_id("appt")
        rebooked = Appointment(
            appointment_id=new_aid,
            patient_id=patient_id,
            starts_at=dt,
            status="booked",
            location_id=location_id,
            practitioner_id=practitioner_id,
            raw={"rebooked_from": appointment_id},
        )
        self.appointments[new_aid] = rebooked
        self._audit(self._actor, "rebook_appointment", "appointment", new_aid, "success")
        return rebooked

    def save_document(
        self,
        patient_id: str,
        *,
        title: str,
        content: bytes,
        content_type: str = "application/pdf",
    ) -> DocumentMeta:
        try:
            assert_allows("nookal.save_document")
        except KillSwitchActive:
            self._audit(
                self._actor,
                "save_document",
                "document",
                patient_id,
                "blocked",
                metadata={"reason": "kill_switch"},
            )
            raise

        meta = DocumentMeta(
            document_id=self._next_id("doc"),
            patient_id=patient_id,
            title=title,
        )
        self.documents.append(meta)
        self._audit(
            self._actor,
            "save_document",
            "document",
            meta.document_id,
            "success",
            metadata={"bytes": len(content), "content_type": content_type},
        )
        return meta

    def upsert_referrer(self, payload: Mapping[str, Any]) -> Referrer:
        try:
            assert_allows("nookal.upsert_referrer")
        except KillSwitchActive:
            self._audit(
                self._actor,
                "upsert_referrer",
                "patient_record",
                str(payload.get("referrer_id", "new")),
                "blocked",
                metadata={"reason": "kill_switch"},
            )
            raise

        rid = str(payload.get("referrer_id") or self._next_id("ref"))
        ref = Referrer(
            referrer_id=rid,
            name=str(payload["name"]),
            provider_number=payload.get("provider_number"),
            raw=dict(payload),
        )
        self.referrers[rid] = ref
        self._audit(self._actor, "upsert_referrer", "patient_record", rid, "success")
        return ref


def build_client(*, use_mock: bool = False) -> NookalClient:
    if use_mock:
        return MockNookalClient()
    return HttpNookalClient()
