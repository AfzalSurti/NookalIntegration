"""
Nookal API wrapper.

Clinic-agnostic: auth, rate limit, retry, audit. No letter/template logic.

Endpoint paths and payload shapes are intentionally incomplete — fill them
from the official Nookal API docs; do not invent plausible request bodies.
"""
from __future__ import annotations

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
    Live HTTP client.

    TODO markers below must be resolved against current Nookal API docs before
    any write path is used against a real clinic account.
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

    def _default_headers(self) -> dict[str, str]:
        if not self._config.api_key:
            # Allow construction without a key (tests / dry-run); calls will fail auth.
            return {"Accept": "application/json"}
        # TODO: confirm auth scheme (Bearer vs custom header) from Nookal docs.
        return {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._config.api_key}",
        }

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

        last_error: Exception | None = None
        for attempt in range(self._config.max_retries + 1):
            self._limiter.take()
            try:
                response = self._http.request(
                    method,
                    path.lstrip("/"),
                    params=params,
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
                if attempt >= self._config.max_retries:
                    break
                self._backoff(attempt)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = NookalServerError(str(exc))
                if attempt >= self._config.max_retries:
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
            raise NookalServerError(f"Nookal {status} on {action}")
        if status >= 400:
            raise NookalError(f"Nookal {status} on {action}")

        if status == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise NookalError(f"non-JSON response on {action}") from exc

    # --- reads ---

    def get_patient(self, patient_id: str) -> PatientRef:
        # TODO: confirm path + response field names from Nookal docs.
        data = self._request(
            "GET",
            f"/patients/{patient_id}",  # TODO: verify
            action="get_patient",
            target_type="patient_record",
            target_id=patient_id,
        )
        return self._parse_patient(data)

    def find_patient_by_phone(self, phone: str) -> list[PatientRef]:
        # TODO: confirm search endpoint + query param name.
        data = self._request(
            "GET",
            "/patients",  # TODO: verify
            action="find_patient_by_phone",
            target_type="patient_record",
            target_id="phone_lookup",
            params={"phone": phone},  # TODO: verify param
        )
        rows = data if isinstance(data, list) else (data or {}).get("data") or []
        return [self._parse_patient(row) for row in rows]

    def list_appointments(
        self,
        *,
        on_date: date | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        patient_id: str | None = None,
    ) -> list[Appointment]:
        params: dict[str, Any] = {}
        # TODO: map these filters to the real query parameters.
        if on_date:
            params["date"] = on_date.isoformat()
        if date_from:
            params["from"] = date_from.isoformat()
        if date_to:
            params["to"] = date_to.isoformat()
        if patient_id:
            params["patient_id"] = patient_id

        data = self._request(
            "GET",
            "/appointments",  # TODO: verify
            action="list_appointments",
            target_type="appointment",
            target_id=patient_id or on_date.isoformat() if on_date else "range",
            params=params,
        )
        rows = data if isinstance(data, list) else (data or {}).get("data") or []
        return [self._parse_appointment(row) for row in rows]

    def get_appointment(self, appointment_id: str) -> Appointment:
        data = self._request(
            "GET",
            f"/appointments/{appointment_id}",  # TODO: verify
            action="get_appointment",
            target_type="appointment",
            target_id=appointment_id,
        )
        return self._parse_appointment(data)

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
        params: dict[str, Any] = {}
        # TODO: confirm which of these filters Nookal supports server-side
        # vs which must be applied client-side after a broader fetch.
        if suburb:
            params["suburb"] = suburb
        if age_min is not None:
            params["age_min"] = age_min
        if age_max is not None:
            params["age_max"] = age_max
        if appointment_from:
            params["appointment_from"] = appointment_from.isoformat()
        if appointment_to:
            params["appointment_to"] = appointment_to.isoformat()
        if referrer_id:
            params["referrer_id"] = referrer_id

        data = self._request(
            "GET",
            "/patients/search",  # TODO: verify — may not exist; might be /patients + filters
            action="search_patients",
            target_type="patient_record",
            target_id="search",
            params=params,
        )
        rows = data if isinstance(data, list) else (data or {}).get("data") or []
        return [self._parse_patient(row) for row in rows]

    def list_referrers(self) -> list[Referrer]:
        data = self._request(
            "GET",
            "/referrers",  # TODO: verify path / resource name
            action="list_referrers",
            target_type="patient_record",
            target_id="referrers",
        )
        rows = data if isinstance(data, list) else (data or {}).get("data") or []
        return [self._parse_referrer(row) for row in rows]

    # --- writes ---

    def update_appointment(
        self,
        appointment_id: str,
        *,
        starts_at: datetime | None = None,
        status: str | None = None,
        **fields: Any,
    ) -> Appointment:
        # TODO: confirm method (PATCH vs PUT), path, and field names.
        body: dict[str, Any] = dict(fields)
        if starts_at is not None:
            body["starts_at"] = starts_at.isoformat()  # TODO: verify field name + format
        if status is not None:
            body["status"] = status

        data = self._request(
            "PATCH",  # TODO: verify
            f"/appointments/{appointment_id}",  # TODO: verify
            action="update_appointment",
            target_type="appointment",
            target_id=appointment_id,
            is_write=True,
            json_body=body,
        )
        return self._parse_appointment(data)

    def create_appointment(self, payload: Mapping[str, Any]) -> Appointment:
        # TODO: replace with documented required fields; do not ship guesses.
        data = self._request(
            "POST",
            "/appointments",  # TODO: verify
            action="create_appointment",
            target_type="appointment",
            target_id=str(payload.get("patient_id", "new")),
            is_write=True,
            json_body=dict(payload),
        )
        return self._parse_appointment(data)

    def save_document(
        self,
        patient_id: str,
        *,
        title: str,
        content: bytes,
        content_type: str = "application/pdf",
    ) -> DocumentMeta:
        # TODO: confirm upload mechanism (multipart, base64 JSON, separate media URL).
        # Intentionally not sending content until docs are confirmed — raise to force review.
        raise NotImplementedError(
            "save_document: wire against Nookal document upload docs before use"
        )

    def upsert_referrer(self, payload: Mapping[str, Any]) -> Referrer:
        # TODO: confirm create vs update endpoints and idempotent upsert behaviour.
        data = self._request(
            "POST",
            "/referrers",  # TODO: verify
            action="upsert_referrer",
            target_type="patient_record",
            target_id=str(payload.get("referrer_id") or payload.get("provider_number") or "new"),
            is_write=True,
            json_body=dict(payload),
        )
        return self._parse_referrer(data)

    # --- parsers (tolerant; adjust when docs arrive) ---

    @staticmethod
    def _parse_patient(data: Any) -> PatientRef:
        if not isinstance(data, Mapping):
            raise NookalValidationError("unexpected patient payload shape")
        pid = data.get("id") or data.get("patient_id") or data.get("PatientID")
        if not pid:
            raise NookalValidationError("patient payload missing id")
        return PatientRef(
            patient_id=str(pid),
            phone=data.get("phone") or data.get("mobile"),
            email=data.get("email"),
            display_name=data.get("name") or data.get("full_name"),
        )

    @staticmethod
    def _parse_appointment(data: Any) -> Appointment:
        if not isinstance(data, Mapping):
            raise NookalValidationError("unexpected appointment payload shape")
        aid = data.get("id") or data.get("appointment_id")
        pid = data.get("patient_id") or data.get("PatientID")
        starts = data.get("starts_at") or data.get("start") or data.get("datetime")
        if not aid or not pid or not starts:
            raise NookalValidationError("appointment payload missing required fields")
        starts_at = starts if isinstance(starts, datetime) else datetime.fromisoformat(str(starts).replace("Z", "+00:00"))
        ends = data.get("ends_at") or data.get("end")
        ends_at = None
        if ends:
            ends_at = ends if isinstance(ends, datetime) else datetime.fromisoformat(str(ends).replace("Z", "+00:00"))
        return Appointment(
            appointment_id=str(aid),
            patient_id=str(pid),
            starts_at=starts_at,
            ends_at=ends_at,
            status=data.get("status"),
            location_id=str(data["location_id"]) if data.get("location_id") else None,
            practitioner_id=str(data["practitioner_id"]) if data.get("practitioner_id") else None,
            raw=dict(data),
        )

    @staticmethod
    def _parse_referrer(data: Any) -> Referrer:
        if not isinstance(data, Mapping):
            raise NookalValidationError("unexpected referrer payload shape")
        rid = data.get("id") or data.get("referrer_id")
        name = data.get("name") or data.get("full_name")
        if not rid or not name:
            raise NookalValidationError("referrer payload missing id/name")
        return Referrer(
            referrer_id=str(rid),
            name=str(name),
            provider_number=data.get("provider_number") or data.get("providerNumber"),
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
        # Reference date for age filters — set by seed helpers for determinism.
        self.as_of = as_of
        self.patients: dict[str, PatientRef] = {}
        self.appointments: dict[str, Appointment] = {}
        self.referrers: dict[str, Referrer] = {}
        self.referrals: dict[str, Referral] = {}
        self.documents: list[DocumentMeta] = []
        # Mock-only availability — not a claimed Nookal API shape.
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
