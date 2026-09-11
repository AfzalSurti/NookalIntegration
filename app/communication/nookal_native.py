"""
Nookal-native communication observer.

Reads communication state from Nookal API response fields (e.g. emailReminderSent).
Does NOT call any invented Nookal messaging endpoint.
Does NOT send messages — Nookal handles all native SMS/email.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.nookal_client import Appointment, NookalClient


@dataclass(frozen=True)
class AppointmentCommunicationState:
    """Observed communication state for a single appointment."""
    appointment_id: str
    email_reminder_sent: bool
    appointment_status: str | None
    cancelled: bool
    dna: bool
    arrived: bool


def observe_appointment(appointment: Appointment) -> AppointmentCommunicationState:
    """
    Observe what Nookal reports about an appointment's communication state.

    Uses only officially documented fields from the Nookal API response.
    The emailReminderSent field indicates whether Nookal's native reminder
    automation has sent an email for this appointment.

    Returns:
        AppointmentCommunicationState with observed fields.
    """
    return AppointmentCommunicationState(
        appointment_id=appointment.appointment_id,
        email_reminder_sent=appointment.email_reminder_sent,
        appointment_status=appointment.status,
        cancelled=appointment.cancelled,
        dna=appointment.dna,
        arrived=appointment.arrived,
    )


def check_nookal_reminder_sent(appointment: Appointment) -> bool:
    """
    Check if Nookal has already sent a reminder for this appointment.

    Uses the emailReminderSent field from the Nookal API.
    Note: Nookal only exposes email reminder status; SMS status is not
    available through the API. This is a known limitation.
    """
    return appointment.email_reminder_sent


def appointment_should_skip_app_reminder(appointment: Appointment) -> bool:
    """
    Determine whether the application should skip sending a reminder.

    Returns True if:
    - Nookal has already sent an email reminder (emailReminderSent)
    - The appointment is cancelled
    - The patient did not attend (DNA)
    - The patient has already arrived
    """
    if appointment.email_reminder_sent:
        return True
    if appointment.cancelled:
        return True
    if appointment.dna:
        return True
    if appointment.arrived:
        return True
    return False
